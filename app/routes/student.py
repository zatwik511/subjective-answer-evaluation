from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from functools import wraps
from datetime import datetime, timezone
from app import db
from app.models import Exam, Submission, Answer, ExamEvent

student_bp = Blueprint('student', __name__, url_prefix='/student')


def student_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'student':
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated


# ── Dashboard ──────────────────────────────────────────────────────────────────

@student_bp.route('/dashboard')
@login_required
@student_required
def dashboard():
    active_exams = Exam.query.filter_by(is_active=True).order_by(Exam.created_at.desc()).all()

    exam_status = {}
    for exam in active_exams:
        sub = Submission.query.filter_by(
            exam_id=exam.id, student_id=current_user.id
        ).first()
        if sub is None or sub.status == 'in_progress':
            exam_status[exam.id] = 'not_started'
        elif sub.released:
            exam_status[exam.id] = 'result_available'
        else:
            exam_status[exam.id] = 'submitted'

    return render_template('student/dashboard.html',
                           active_exams=active_exams,
                           exam_status=exam_status)


# ── Take exam ──────────────────────────────────────────────────────────────────

@student_bp.route('/exam/<int:exam_id>')
@login_required
@student_required
def take_exam(exam_id):
    exam = Exam.query.filter_by(id=exam_id, is_active=True).first_or_404()

    sub = Submission.query.filter_by(
        exam_id=exam_id, student_id=current_user.id
    ).first()

    # Already fully submitted — show confirmation
    if sub and sub.status != 'in_progress':
        flash('You have already submitted this exam.', 'info')
        return redirect(url_for('student.submitted', submission_id=sub.id))

    # Create a pending submission on first visit so we have an ID for event logging
    if sub is None:
        sub = Submission(
            exam_id=exam_id,
            student_id=current_user.id,
            status='in_progress'
        )
        db.session.add(sub)
        db.session.commit()

    return render_template('student/take_exam.html', exam=exam, submission=sub)


# ── Submit exam ────────────────────────────────────────────────────────────────

@student_bp.route('/exam/<int:exam_id>/submit', methods=['POST'])
@login_required
@student_required
def submit_exam(exam_id):
    exam = Exam.query.filter_by(id=exam_id, is_active=True).first_or_404()

    sub = Submission.query.filter_by(
        exam_id=exam_id, student_id=current_user.id
    ).first()

    # Already finalised — redirect without touching DB
    if sub and sub.status != 'in_progress':
        return redirect(url_for('student.submitted', submission_id=sub.id))

    # Should always exist (created on GET), but create if missing
    if sub is None:
        sub = Submission(
            exam_id=exam_id,
            student_id=current_user.id,
            status='in_progress'
        )
        db.session.add(sub)
        db.session.flush()

    # Aggregate cheating signals from logged events
    events = ExamEvent.query.filter_by(submission_id=sub.id).all()

    tab_switch_count = sum(1 for e in events if e.event_type == 'tab_switch')
    paste_by_question = {}
    for e in events:
        if e.event_type == 'paste' and e.question_id:
            paste_by_question[e.question_id] = paste_by_question.get(e.question_id, 0) + 1

    # Save answers
    for question in exam.questions:
        answer_text = request.form.get(f'answer_{question.id}', '').strip()
        answer = Answer(
            submission_id=sub.id,
            question_id=question.id,
            answer_text=answer_text or None,
            tab_switches=tab_switch_count,
            paste_events=paste_by_question.get(question.id, 0)
        )
        db.session.add(answer)

    sub.status = 'pending'
    sub.submitted_at = datetime.now(timezone.utc)
    db.session.commit()

    # Run cross-student similarity check after commit
    _check_similarity(sub)

    return redirect(url_for('student.submitted', submission_id=sub.id))


# ── Submitted confirmation ─────────────────────────────────────────────────────

@student_bp.route('/submission/<int:submission_id>')
@login_required
@student_required
def submitted(submission_id):
    submission = Submission.query.filter_by(
        id=submission_id, student_id=current_user.id
    ).first_or_404()
    return render_template('student/submitted.html', submission=submission)


# ── Cross-student similarity check ────────────────────────────────────────────

def _check_similarity(new_submission):
    """Flag submissions where any answer pair exceeds 0.85 cosine similarity."""
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity

    exam_id = new_submission.exam_id
    other_submissions = Submission.query.filter(
        Submission.exam_id == exam_id,
        Submission.id != new_submission.id,
        Submission.status.in_(['pending', 'graded', 'flagged'])
    ).all()

    if not other_submissions:
        return

    new_answers = {a.question_id: a.answer_text or '' for a in new_submission.answers}

    for other_sub in other_submissions:
        other_answers = {a.question_id: a.answer_text or '' for a in other_sub.answers}

        for qid, new_text in new_answers.items():
            other_text = other_answers.get(qid, '')
            if not new_text.strip() or not other_text.strip():
                continue

            try:
                vec = TfidfVectorizer().fit_transform([new_text, other_text])
                sim = cosine_similarity(vec[0], vec[1])[0][0]
            except Exception:
                continue

            if sim >= 0.85:
                new_submission.status = 'flagged'
                other_sub.status = 'flagged'
                db.session.commit()
                return  # one flag is enough per submission pair
