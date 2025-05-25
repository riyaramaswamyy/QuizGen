import os
import logging
from flask import Flask, render_template, request, send_file, flash, session, jsonify, redirect, url_for
from werkzeug.utils import secure_filename
from utils.ppt_processor import process_powerpoint
import json
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import func

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

class Base(DeclarativeBase):
    pass

db = SQLAlchemy(model_class=Base)
app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET")

# Configure database
database_url = os.environ.get("DATABASE_URL")
if database_url:
    # Use regular connection URL instead of pooler to avoid potential DNS issues
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_recycle": 300,
        "pool_pre_ping": True,
        "pool_size": 5,
        "max_overflow": 10,
    }

# Configure upload settings
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'pptx'}

# Ensure upload folder exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Initialize the database
db.init_app(app)

with app.app_context():
    import models  # Import models after db is initialized
    db.create_all()  # Create tables

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/')
def index():
    from models import Flashcard
    
    # Check if there are any flashcards in the database
    has_cards = Flashcard.query.count() > 0
    
    # Always check for cards, but use param to determine if we should prioritize card view
    show_cards = request.args.get('show_cards', 'false') == 'true' or has_cards
    
    if show_cards:
        # Get all presentations from the database
        presentations = db.session.query(Flashcard.presentation_name).distinct().all()
        presentations = [p[0] for p in presentations]
        
        # Group flashcards by presentation
        grouped_flashcards = {}
        for presentation in presentations:
            # Get cards for this presentation
            presentation_cards = Flashcard.query.filter_by(presentation_name=presentation).all()
            grouped_flashcards[presentation] = [card.to_dict() for card in presentation_cards]
        
        # Get total count
        total_count = Flashcard.query.count()
        
        # For backward compatibility with the JavaScript
        all_flashcards = [card.to_dict() for card in Flashcard.query.order_by(Flashcard.created_at.desc()).all()]
        
        return render_template('index.html', 
                              flashcards=all_flashcards if all_flashcards else None,
                              grouped_flashcards=grouped_flashcards, 
                              total_count=total_count,
                              presentations=presentations,
                              show_cards=True,
                              has_cards=has_cards)
    
    # No cards in database, show upload form
    return render_template('index.html', flashcards=None, show_cards=False, has_cards=False)

@app.route('/upload', methods=['POST'])
def upload_file():
    logger.debug("Upload request received")
    logger.debug(f"Files in request: {request.files}")
    logger.debug(f"Form data: {request.form}")
    logger.debug(f"Request method: {request.method}")
    
    # Add extra logging to see what's happening
    for name, file_storage in request.files.items():
        logger.debug(f"File {name}: {file_storage.filename}")

    if 'file' not in request.files:
        logger.error("No file part in the request")
        flash('No file selected', 'error')
        return render_template('index.html', has_cards=False, show_cards=False)

    file = request.files['file']
    logger.debug(f"File received: {file.filename}")

    if file.filename == '':
        logger.error("No file selected")
        flash('No file selected', 'error')
        return render_template('index.html', has_cards=False, show_cards=False)

    if file and allowed_file(file.filename):
        if file.filename:
            try:
                filename = secure_filename(file.filename)
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                logger.debug(f"Saving file to: {filepath}")
                file.save(filepath)
                logger.debug(f"File saved successfully to {filepath}")
            except Exception as e:
                logger.error(f"Error saving file: {e}")
                flash(f'Error saving file: {e}', 'error')
                return render_template('index.html', has_cards=False, show_cards=False)
        else:
            flash('Invalid filename', 'error')
            return render_template('index.html', has_cards=False, show_cards=False)

        try:
            logging.info(f"Processing file: {filename}")
            flashcards = process_powerpoint(filepath)

            if os.path.exists(filepath):
                os.remove(filepath)  # Clean up the uploaded file
                logger.debug("Cleaned up uploaded file")

            if not flashcards:
                flash('No educational content found in the presentation. The system looks for vocabulary terms, mathematical formulas, and practice problems.', 'warning')
                return render_template('index.html')

            # Handle the large session data issue - store only essential info
            # Trim extra-long definitions to avoid session cookie limit
            for card in flashcards:
                # Limit the size of 'back' (definition/answer) to 300 chars if needed
                if len(card['back']) > 300:
                    card['back'] = card['back'][:297] + '...'
                    
            # Store flashcards in session for review
            session['pending_flashcards'] = flashcards
            session['presentation_name'] = filename

            # Count items by type
            counts = {}
            for card in flashcards:
                counts[card['type']] = counts.get(card['type'], 0) + 1

            # Create summary message
            summary = []
            type_names = {
                'vocabulary': 'vocabulary terms',
                'formula': 'mathematical formulas',
                'problem': 'practice problems'
            }
            for type_key, count in counts.items():
                if count > 0:
                    summary.append(f"{count} {type_names[type_key]}")

            summary_text = ', '.join(summary[:-1])
            if len(summary) > 1:
                summary_text += f" and {summary[-1]}"
            else:
                summary_text = summary[0]

            flash(f'Successfully extracted {summary_text}! Please review and select the flashcards you want to keep.', 'success')
            
            # Redirect to the review page
            return redirect(url_for('review_flashcards'))
        except Exception as e:
            logger.error(f"Error processing PowerPoint: {str(e)}")
            flash('Error processing PowerPoint file. Please ensure the file is not corrupted.', 'error')
            if os.path.exists(filepath):
                os.remove(filepath)
            return render_template('index.html')
    else:
        flash('Invalid file type. Please upload a .pptx file', 'error')
        return render_template('index.html')

@app.route('/review', methods=['GET'])
def review_flashcards():
    """Show a review screen for extracted flashcards before saving them"""
    # Get flashcards from session
    pending_flashcards = session.get('pending_flashcards', [])
    
    if not pending_flashcards:
        flash('No flashcards to review. Please upload a PowerPoint file.', 'warning')
        return redirect(url_for('index'))
    
    return render_template('review.html', flashcards=pending_flashcards)

@app.route('/save_flashcards', methods=['POST'])
def save_flashcards():
    """Save selected flashcards and any custom ones"""
    try:
        # Get selected flashcard indices and additional flashcards
        selected_indices = request.form.getlist('selected_cards')
        logger.debug(f"Selected indices: {selected_indices}")
        
        # Get original flashcards from session
        all_flashcards = session.get('pending_flashcards', [])
        presentation_name = session.get('presentation_name', 'custom')
        
        if not all_flashcards:
            flash('No flashcards found in session. Please try uploading again.', 'error')
            return redirect(url_for('index'))
            
        # Convert to integers
        try:
            selected_indices = [int(idx) for idx in selected_indices]
        except ValueError:
            logger.error("Invalid indices received")
            selected_indices = []
        
        if not selected_indices:
            flash('No flashcards were selected to save.', 'warning')
            return redirect(url_for('index'))
        
        # Get selected flashcards based on indices (only include valid indices)
        selected_flashcards = []
        for i in selected_indices:
            if i < len(all_flashcards):
                selected_flashcards.append(all_flashcards[i])
        
        # Process any custom flashcards
        custom_fronts = request.form.getlist('custom_front')
        custom_backs = request.form.getlist('custom_back')
        custom_types = request.form.getlist('custom_type')
        
        logger.debug(f"Custom fronts: {len(custom_fronts)}, backs: {len(custom_backs)}, types: {len(custom_types)}")
        
        for i in range(len(custom_fronts)):
            if i < len(custom_backs) and i < len(custom_types):
                front = custom_fronts[i].strip()
                back = custom_backs[i].strip()
                
                if front and back:  # Only add if both fields have content
                    selected_flashcards.append({
                        'type': custom_types[i],
                        'front': front,
                        'back': back
                    })
        
        # Save to database
        if selected_flashcards:
            from models import Flashcard
            
            # First, clear any existing flashcards for this presentation if needed
            # This ensures we only get exactly the cards we want
            if request.form.get('clear_existing') == 'true':
                existing = Flashcard.query.filter_by(presentation_name=presentation_name).all()
                for card in existing:
                    db.session.delete(card)
                db.session.commit()
            
            # Prevent duplicate flashcards
            existing_count = 0
            new_count = 0
            
            for card_data in selected_flashcards:
                # Check for exact duplicate within this presentation
                existing = Flashcard.query.filter_by(
                    front=card_data['front'],
                    presentation_name=presentation_name
                ).first()
                
                if existing:
                    existing_count += 1
                    continue
                
                # No duplicates found, proceed with creating a new flashcard
                card = Flashcard()
                card.type = card_data['type']
                card.front = card_data['front']
                card.back = card_data['back']
                card.presentation_name = presentation_name
                db.session.add(card)
                new_count += 1
            
            db.session.commit()
            logger.debug(f"Saved {new_count} new flashcards to database (skipped {existing_count} duplicates)")
            
            # Clear session data
            session.pop('pending_flashcards', None)
            session.pop('presentation_name', None)
            
            # Redirect to index with success message
            if existing_count > 0:
                flash(f'Successfully saved {new_count} new flashcards! (Skipped {existing_count} duplicates)', 'success')
            else:
                flash(f'Successfully saved {new_count} flashcards!', 'success')
            return redirect(url_for('index', show_cards='true'))
        else:
            flash('No flashcards were selected to save.', 'warning')
            return redirect(url_for('index'))
    
    except Exception as e:
        logger.error(f"Error saving flashcards: {str(e)}")
        flash(f'Error saving flashcards: {str(e)}', 'error')
        return redirect(url_for('review_flashcards'))

@app.route('/generate_ai_cards', methods=['POST'])
def generate_ai_cards():
    """Generate flashcards based on a topic using AI"""
    topic = request.form.get('topic', '').strip()
    if not topic:
        return jsonify({
            'success': False,
            'message': 'Please enter a topic to generate flashcards'
        })
    
    try:
        # Import AI processing from utils
        from utils.ppt_processor import generate_topic_flashcards
        generated_cards = generate_topic_flashcards(topic)
        
        if generated_cards:
            # Get pending flashcards from session and update
            pending_flashcards = session.get('pending_flashcards', [])
            
            # Trim any long definitions to prevent session cookie limit
            for card in generated_cards:
                # Limit the size of 'back' (definition/answer) to 300 chars if needed
                if len(card['back']) > 300:
                    card['back'] = card['back'][:297] + '...'
            
            # Find where to insert the new cards
            current_length = len(pending_flashcards)
            # Add the new cards
            pending_flashcards.extend(generated_cards)
            # Update session
            session['pending_flashcards'] = pending_flashcards
            
            # Return success response with the new cards
            return jsonify({
                'success': True,
                'flashcards': generated_cards,
                'message': f'Successfully generated {len(generated_cards)} flashcards about "{topic}"!'
            })
        else:
            return jsonify({
                'success': False,
                'message': f'Could not generate flashcards about "{topic}". Please try a different topic.'
            })
    except Exception as e:
        logger.error(f"Error generating AI flashcards: {str(e)}")
        return jsonify({
            'success': False,
            'message': f'Error generating flashcards: {str(e)}'
        })

@app.route('/download')
def download_flashcards():
    from models import Flashcard
    # Get all flashcards from the database
    all_flashcards = [card.to_dict() for card in Flashcard.query.all()]

    if not all_flashcards:
        flash('No flashcards available to download', 'error')
        return render_template('index.html')

    # Create a JSON file with the flashcards
    filename = '/tmp/flashcards.json'
    with open(filename, 'w') as f:
        json.dump(all_flashcards, f)

    return send_file(filename, as_attachment=True, download_name='flashcards.json')
    
@app.route('/reset_all', methods=['GET'])
def reset_all():
    """Complete system reset - clears database and session cache"""
    try:
        # Clear all session data
        session.clear()
        
        # Clear database
        from models import Flashcard
        count = Flashcard.query.count()
        Flashcard.query.delete()
        db.session.commit()
        
        flash(f'System reset complete! Cleared {count} flashcards and all session data.', 'success')
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error during system reset: {str(e)}")
        flash(f'Error during system reset: {str(e)}', 'error')
    
    return redirect(url_for('index'))

@app.route('/delete_all_flashcards', methods=['POST'])
def delete_all_flashcards():
    """Delete all flashcards from the database"""
    try:
        from models import Flashcard
        # Delete all flashcards
        num_deleted = Flashcard.query.delete()
        db.session.commit()
        flash(f'Successfully deleted all {num_deleted} flashcards!', 'success')
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting flashcards: {str(e)}")
        flash(f'Error deleting flashcards: {str(e)}', 'error')
    
    return redirect(url_for('index'))
    
@app.route('/delete_presentation_flashcards/<presentation_name>', methods=['POST'])
def delete_presentation_flashcards(presentation_name):
    """Delete all flashcards for a specific presentation"""
    try:
        from models import Flashcard
        
        # Count cards to delete for this presentation
        count = Flashcard.query.filter_by(presentation_name=presentation_name).count()
        
        # Delete cards for this presentation
        Flashcard.query.filter_by(presentation_name=presentation_name).delete()
        db.session.commit()
        
        # Clear session data if it matches this presentation
        if 'presentation_name' in session and session['presentation_name'] == presentation_name:
            session.pop('pending_flashcards', None)
            session.pop('presentation_name', None)
        
        flash(f'Successfully deleted {count} flashcards from "{presentation_name}"!', 'success')
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting presentation flashcards: {str(e)}")
        flash(f'Error deleting flashcards: {str(e)}', 'error')
    
    return redirect(url_for('index'))
    
@app.route('/remove_duplicates', methods=['POST'])
def remove_duplicates():
    """Remove duplicate flashcards from the database"""
    try:
        from models import Flashcard
        
        # Get all presentations
        presentations = db.session.query(Flashcard.presentation_name).distinct().all()
        presentations = [p[0] for p in presentations]
        
        total_removed = 0
        
        # First, let's remove exact duplicates across the entire database
        # This will handle duplicates regardless of presentation name, date, or ID
        all_cards = Flashcard.query.all()
        
        # Track unique content
        seen_cards = {}  # key: (front + back + type), value: card
        
        for card in all_cards:
            # Create a key based on content only
            content_key = f"{card.front.strip()}|{card.back.strip()}|{card.type.strip()}"
            
            if content_key in seen_cards:
                # This is a duplicate based on content
                db.session.delete(card)
                total_removed += 1
            else:
                seen_cards[content_key] = card
        
        # Then, perform presentation-specific deduplication
        for presentation in presentations:
            # Find all cards remaining for this presentation
            cards = Flashcard.query.filter_by(presentation_name=presentation).all()
            
            # Track unique card fronts (titles) within this presentation
            unique_fronts = {}
            
            for card in cards:
                # If we've already seen this front (term) in this presentation, keep only the first one
                if card.front in unique_fronts:
                    # Delete this duplicate
                    db.session.delete(card)
                    total_removed += 1
                else:
                    # First time seeing this front, remember it
                    unique_fronts[card.front] = card
        
        db.session.commit()
        flash(f'Successfully removed {total_removed} duplicate flashcards!', 'success')
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error removing duplicate flashcards: {str(e)}")
        flash(f'Error removing duplicate flashcards: {str(e)}', 'error')
    
    return redirect(url_for('index'))
