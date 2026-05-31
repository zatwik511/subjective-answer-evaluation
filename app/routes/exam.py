import json
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
from app import db
from app.models import Submission, ExamEvent

exam_bp = Blueprint('exam', __name__, url_prefix='/exam')


@exam_bp.route('/log_event', methods=['POST'])
@login_required
def log_event():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'No data'}), 400

    exam_id = data.get('exam_id')
    event_type = data.get('event_type')
    question_id = data.get('question_id')    # optional
    metadata = data.get('metadata', {})

    if not exam_id or not event_type:
        return jsonify({'error': 'Missing exam_id or event_type'}), 400

    # Find the in-progress submission for this student + exam
    sub = Submission.query.filter_by(
        exam_id=exam_id,
        student_id=current_user.id,
        status='in_progress'
    ).first()

    if not sub:
        return jsonify({'error': 'No active submission'}), 404

    event = ExamEvent(
        submission_id=sub.id,
        event_type=event_type,
        question_id=question_id,
        metadata_=json.dumps(metadata),
        timestamp=datetime.now(timezone.utc)
    )
    db.session.add(event)
    db.session.commit()

    return jsonify({'status': 'logged'}), 200
