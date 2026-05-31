from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from functools import wraps
from app import db
from app.models import Exam, Question, Submission, Answer
from app.grading import grade_answer, detect_ai_generated

teacher_bp = Blueprint('teacher', __name__, url_prefix='/teacher')


def teacher_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'teacher':
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated


# ── Dashboard ──────────────────────────────────────────────────────────────────

@teacher_bp.route('/dashboard')
@login_required
@teacher_required
def dashboard():
    exams = Exam.query.filter_by(teacher_id=current_user.id).order_by(Exam.created_at.desc()).all()
    total_submissions = sum(len(e.submissions) for e in exams)
    pending_grading = sum(
        1 for e in exams for s in e.submissions if s.status == 'pending'
    )
    return render_template('teacher/dashboard.html', exams=exams,
                           total_submissions=total_submissions,
                           pending_grading=pending_grading)


# ── Exam list ──────────────────────────────────────────────────────────────────

@teacher_bp.route('/exams')
@login_required
@teacher_required
def exams():
    all_exams = Exam.query.filter_by(teacher_id=current_user.id)\
                          .order_by(Exam.created_at.desc()).all()
    return render_template('teacher/exams.html', exams=all_exams)


# ── Create exam ────────────────────────────────────────────────────────────────

@teacher_bp.route('/exams/create', methods=['GET', 'POST'])
@login_required
@teacher_required
def create_exam():
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        subject = request.form.get('subject', '').strip()
        time_limit = request.form.get('time_limit', '60').strip()

        if not title or not subject:
            flash('Title and subject are required.', 'danger')
            return render_template('teacher/create_exam.html')

        try:
            time_limit = int(time_limit)
            if time_limit < 1:
                raise ValueError
        except ValueError:
            flash('Time limit must be a positive number.', 'danger')
            return render_template('teacher/create_exam.html')

        exam = Exam(title=title, subject=subject,
                    time_limit_minutes=time_limit,
                    teacher_id=current_user.id)
        db.session.add(exam)
        db.session.commit()

        flash(f'Exam "{title}" created. Now add your questions.', 'success')
        return redirect(url_for('teacher.edit_exam', exam_id=exam.id))

    return render_template('teacher/create_exam.html')


# ── Edit exam / question builder ───────────────────────────────────────────────

@teacher_bp.route('/exams/<int:exam_id>/edit')
@login_required
@teacher_required
def edit_exam(exam_id):
    exam = Exam.query.filter_by(id=exam_id, teacher_id=current_user.id).first_or_404()
    return render_template('teacher/edit_exam.html', exam=exam)


@teacher_bp.route('/exams/<int:exam_id>/question/add', methods=['POST'])
@login_required
@teacher_required
def add_question(exam_id):
    exam = Exam.query.filter_by(id=exam_id, teacher_id=current_user.id).first_or_404()

    text = request.form.get('text', '').strip()
    qtype = request.form.get('type', 'short')
    max_marks = request.form.get('max_marks', '10').strip()
    model_answer = request.form.get('model_answer', '').strip()
    rubric = request.form.get('rubric', '').strip()

    if not text:
        flash('Question text is required.', 'danger')
        return redirect(url_for('teacher.edit_exam', exam_id=exam_id))

    try:
        max_marks = int(max_marks)
        if max_marks < 1:
            raise ValueError
    except ValueError:
        flash('Max marks must be a positive number.', 'danger')
        return redirect(url_for('teacher.edit_exam', exam_id=exam_id))

    next_order = db.session.query(db.func.max(Question.order))\
                           .filter_by(exam_id=exam_id).scalar() or 0

    question = Question(
        exam_id=exam_id,
        text=text,
        type=qtype,
        max_marks=max_marks,
        model_answer=model_answer or None,
        rubric=rubric or None,
        order=next_order + 1
    )
    db.session.add(question)
    db.session.commit()

    flash('Question added.', 'success')
    return redirect(url_for('teacher.edit_exam', exam_id=exam_id))


@teacher_bp.route('/exams/<int:exam_id>/question/<int:q_id>/delete', methods=['POST'])
@login_required
@teacher_required
def delete_question(exam_id, q_id):
    exam = Exam.query.filter_by(id=exam_id, teacher_id=current_user.id).first_or_404()
    question = Question.query.filter_by(id=q_id, exam_id=exam_id).first_or_404()
    db.session.delete(question)
    db.session.commit()
    _reorder_questions(exam_id)
    flash('Question deleted.', 'info')
    return redirect(url_for('teacher.edit_exam', exam_id=exam_id))


@teacher_bp.route('/exams/<int:exam_id>/question/<int:q_id>/move', methods=['POST'])
@login_required
@teacher_required
def move_question(exam_id, q_id):
    exam = Exam.query.filter_by(id=exam_id, teacher_id=current_user.id).first_or_404()
    direction = request.form.get('direction')
    questions = Question.query.filter_by(exam_id=exam_id).order_by(Question.order).all()

    idx = next((i for i, q in enumerate(questions) if q.id == q_id), None)
    if idx is None:
        return redirect(url_for('teacher.edit_exam', exam_id=exam_id))

    swap_idx = idx - 1 if direction == 'up' else idx + 1
    if 0 <= swap_idx < len(questions):
        questions[idx].order, questions[swap_idx].order = \
            questions[swap_idx].order, questions[idx].order
        db.session.commit()

    return redirect(url_for('teacher.edit_exam', exam_id=exam_id))


@teacher_bp.route('/exams/<int:exam_id>/question/<int:q_id>/edit', methods=['POST'])
@login_required
@teacher_required
def edit_question(exam_id, q_id):
    exam = Exam.query.filter_by(id=exam_id, teacher_id=current_user.id).first_or_404()
    question = Question.query.filter_by(id=q_id, exam_id=exam_id).first_or_404()

    text = request.form.get('text', '').strip()
    qtype = request.form.get('type', question.type)
    max_marks = request.form.get('max_marks', str(question.max_marks)).strip()
    model_answer = request.form.get('model_answer', '').strip()
    rubric = request.form.get('rubric', '').strip()

    if not text:
        flash('Question text cannot be empty.', 'danger')
        return redirect(url_for('teacher.edit_exam', exam_id=exam_id))

    try:
        max_marks = int(max_marks)
        if max_marks < 1:
            raise ValueError
    except ValueError:
        flash('Max marks must be a positive number.', 'danger')
        return redirect(url_for('teacher.edit_exam', exam_id=exam_id))

    question.text = text
    question.type = qtype
    question.max_marks = max_marks
    question.model_answer = model_answer or None
    question.rubric = rubric or None
    db.session.commit()

    flash('Question updated.', 'success')
    return redirect(url_for('teacher.edit_exam', exam_id=exam_id))


# ── Publish / unpublish ────────────────────────────────────────────────────────

@teacher_bp.route('/exams/<int:exam_id>/toggle', methods=['POST'])
@login_required
@teacher_required
def toggle_exam(exam_id):
    exam = Exam.query.filter_by(id=exam_id, teacher_id=current_user.id).first_or_404()

    if not exam.is_active and len(exam.questions) == 0:
        flash('Add at least one question before publishing.', 'warning')
        return redirect(url_for('teacher.edit_exam', exam_id=exam_id))

    exam.is_active = not exam.is_active
    db.session.commit()

    status = 'published and is now Live' if exam.is_active else 'unpublished and set to Draft'
    flash(f'"{exam.title}" {status}.', 'success')
    return redirect(url_for('teacher.exams'))


# ── Exam detail (submissions) ──────────────────────────────────────────────────

@teacher_bp.route('/exams/<int:exam_id>')
@login_required
@teacher_required
def exam_detail(exam_id):
    exam = Exam.query.filter_by(id=exam_id, teacher_id=current_user.id).first_or_404()
    submissions = Submission.query.filter_by(exam_id=exam_id)\
                                  .order_by(Submission.submitted_at.desc()).all()
    return render_template('teacher/exam_detail.html', exam=exam, submissions=submissions)


# ── Grading ────────────────────────────────────────────────────────────────────

@teacher_bp.route('/grade/<int:submission_id>', methods=['POST'])
@login_required
@teacher_required
def grade_submission(submission_id):
    submission = Submission.query.get_or_404(submission_id)
    exam = Exam.query.filter_by(id=submission.exam_id, teacher_id=current_user.id).first_or_404()

    try:
        total = 0.0
        for answer in submission.answers:
            result = grade_answer(answer.question, answer.answer_text)
            ai_prob = detect_ai_generated(answer.answer_text)
            answer.score = result['score']
            answer.feedback = result['feedback']
            answer.ai_flag_score = ai_prob
            total += result['score']

        submission.total_score = total
        submission.status = 'graded'
        db.session.commit()
        flash(f'Submission graded. Total score: {total:.1f}', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Grading failed: {e}', 'danger')

    return redirect(url_for('teacher.exam_detail', exam_id=submission.exam_id))


@teacher_bp.route('/grade_all/<int:exam_id>', methods=['POST'])
@login_required
@teacher_required
def grade_all(exam_id):
    exam = Exam.query.filter_by(id=exam_id, teacher_id=current_user.id).first_or_404()
    pending = Submission.query.filter_by(exam_id=exam_id, status='pending').all()

    if not pending:
        flash('No pending submissions to grade.', 'info')
        return redirect(url_for('teacher.exam_detail', exam_id=exam_id))

    graded_count = 0
    errors = 0
    for submission in pending:
        try:
            total = 0.0
            for answer in submission.answers:
                result = grade_answer(answer.question, answer.answer_text)
                ai_prob = detect_ai_generated(answer.answer_text)
                answer.score = result['score']
                answer.feedback = result['feedback']
                answer.ai_flag_score = ai_prob
                total += result['score']
            submission.total_score = total
            submission.status = 'graded'
            db.session.commit()
            graded_count += 1
        except Exception:
            db.session.rollback()
            errors += 1

    msg = f'Graded {graded_count} submission(s).'
    if errors:
        msg += f' {errors} failed — check API key.'
    flash(msg, 'success' if not errors else 'warning')
    return redirect(url_for('teacher.exam_detail', exam_id=exam_id))


# ── Delete exam ────────────────────────────────────────────────────────────────

@teacher_bp.route('/exams/<int:exam_id>/delete', methods=['POST'])
@login_required
@teacher_required
def delete_exam(exam_id):
    exam = Exam.query.filter_by(id=exam_id, teacher_id=current_user.id).first_or_404()
    db.session.delete(exam)
    db.session.commit()
    flash(f'Exam "{exam.title}" deleted.', 'info')
    return redirect(url_for('teacher.exams'))


# ── Helpers ────────────────────────────────────────────────────────────────────

def _reorder_questions(exam_id):
    questions = Question.query.filter_by(exam_id=exam_id).order_by(Question.order).all()
    for i, q in enumerate(questions, start=1):
        q.order = i
    db.session.commit()
