from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from functools import wraps
from datetime import datetime, timezone
from app import db
from app.models import Exam, Question, Submission, Answer

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

    # Tag each exam with this student's submission status
    exam_status = {}
    for exam in active_exams:
        sub = Submission.query.filter_by(
            exam_id=exam.id, student_id=current_user.id
        ).first()
        if sub is None:
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

    existing = Submission.query.filter_by(
        exam_id=exam_id, student_id=current_user.id
    ).first()

    if existing:
        flash('You have already submitted this exam.', 'info')
        return redirect(url_for('student.submitted', submission_id=existing.id))

    return render_template('student/take_exam.html', exam=exam)


# ── Submit exam ────────────────────────────────────────────────────────────────

@student_bp.route('/exam/<int:exam_id>/submit', methods=['POST'])
@login_required
@student_required
def submit_exam(exam_id):
    exam = Exam.query.filter_by(id=exam_id, is_active=True).first_or_404()

    # Prevent double submission
    existing = Submission.query.filter_by(
        exam_id=exam_id, student_id=current_user.id
    ).first()
    if existing:
        return redirect(url_for('student.submitted', submission_id=existing.id))

    submission = Submission(
        exam_id=exam_id,
        student_id=current_user.id,
        submitted_at=datetime.now(timezone.utc),
        status='pending'
    )
    db.session.add(submission)
    db.session.flush()  # get submission.id before commit

    for question in exam.questions:
        answer_text = request.form.get(f'answer_{question.id}', '').strip()
        answer = Answer(
            submission_id=submission.id,
            question_id=question.id,
            answer_text=answer_text or None
        )
        db.session.add(answer)

    db.session.commit()
    return redirect(url_for('student.submitted', submission_id=submission.id))


# ── Submitted confirmation ─────────────────────────────────────────────────────

@student_bp.route('/submission/<int:submission_id>')
@login_required
@student_required
def submitted(submission_id):
    submission = Submission.query.filter_by(
        id=submission_id, student_id=current_user.id
    ).first_or_404()
    return render_template('student/submitted.html', submission=submission)
