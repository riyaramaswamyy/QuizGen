from app import db
from datetime import datetime


class Flashcard(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(20), nullable=False)  # vocabulary, formula, or problem
    front = db.Column(db.Text, nullable=False)
    back = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    presentation_name = db.Column(db.String(255))  # to group cards by presentation

    def to_dict(self):
        return {
            'type': self.type,
            'front': self.front,
            'back': self.back
        }
