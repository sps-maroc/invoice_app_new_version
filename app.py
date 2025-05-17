import os
import json
import sqlite3
import zipfile
import logging
import sys
import shutil
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for, session, flash
from werkzeug.utils import secure_filename
import mimetypes
import time
import uuid
import requests
from pathlib import Path
import re
import math
import traceback
import io
import email
import imaplib
from email.header import decode_header
import socket

# Import the invoice scanner
from invoice_scanner import InvoiceScanner, InvoiceDatabase

# Import utility modules
from utils.database import (
    get_db_connection, check_and_update_schema, initialize_shadow_table, 
    initialize_tables, check_invoice_exists, save_to_pending, update_pending_invoice
)
from utils.file_utils import (
    sanitize_filename, allowed_file, cleanup_uploaded_files, 
    organize_file, cleanup_processed_files, check_for_duplicate_invoice,
    _parse_amount
)
from utils.ai_utils import select_ai_model
from utils.processing.invoice_processor import process_invoice_file

# Import email functions
from utils.email_utils import (
    imap_connection_wrapper, save_email_credentials, get_email_credentials,
    delete_email_credentials, cleanup_expired_connections, connect_to_email,
    disconnect_email, cleanup_email_attachments
)

# Setup logging
log = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

# Create logs directory if it doesn't exist
logs_dir = os.path.join(os.path.dirname(__file__), 'logs')
os.makedirs(logs_dir, exist_ok=True)

# Add file handler for app logs
log_file = os.path.join(logs_dir, f"invoice_app_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
file_handler = logging.FileHandler(log_file)
file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
logging.getLogger().addHandler(file_handler)

# Get absolute paths for app directories
app_root_dir = os.path.dirname(os.path.abspath(__file__))
uploads_dir = os.path.join(app_root_dir, 'uploads')
preview_dir = os.path.join(uploads_dir, 'preview')
archive_dir = os.path.join(app_root_dir, 'organized_invoices')
db_path = os.path.join(app_root_dir, 'invoices.db')
email_attachments_dir = os.path.join(uploads_dir, 'email_attachments')
lexoffice_dir = os.path.join(uploads_dir, 'lexoffice')

# Initialize Flask app
app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = uploads_dir
app.config['PREVIEW_FOLDER'] = preview_dir
app.config['TEMP_FOLDER'] = os.path.join(uploads_dir, 'temp')
app.config['ARCHIVE_DIR'] = archive_dir
app.config['DATABASE'] = db_path
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max upload size
app.config['ALLOWED_EXTENSIONS'] = {'pdf'}
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'invoice-app-dev-key')
app.config['EMAIL_SESSION_TIMEOUT'] = 3600  # 1 hour
app.config['EMAIL_ATTACHMENTS_FOLDER'] = email_attachments_dir
app.config['LEXOFFICE_FOLDER'] = lexoffice_dir

# Add 'now' variable to all templates
@app.context_processor
def inject_now():
    return {'now': datetime.now()}

# Ensure required directories exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['PREVIEW_FOLDER'], exist_ok=True)
os.makedirs(app.config['TEMP_FOLDER'], exist_ok=True)
os.makedirs(app.config['ARCHIVE_DIR'], exist_ok=True)

# Log startup information
log.info(f"Starting invoice app with database: {app.config['DATABASE']}")
log.info(f"Archive directory: {app.config['ARCHIVE_DIR']}")
log.info(f"Email attachments folder: {app.config['EMAIL_ATTACHMENTS_FOLDER']}")
log.info(f"LexOffice downloads folder: {app.config['LEXOFFICE_FOLDER']}")

# Store active email connections
email_connections = {}

# Create required folders if they don't exist
for directory in [
    app.config['UPLOAD_FOLDER'],
    app.config['PREVIEW_FOLDER'],
    app.config['ARCHIVE_DIR'],
    app.config['EMAIL_ATTACHMENTS_FOLDER'],
    app.config['LEXOFFICE_FOLDER'],
    os.path.join(app.config['ARCHIVE_DIR'], 'by_supplier'),
    os.path.join(app.config['ARCHIVE_DIR'], 'by_date'),
    os.path.join(app.config['ARCHIVE_DIR'], 'by_company'),
]:
    os.makedirs(directory, exist_ok=True)
    log.info(f"Created directory: {directory}")

# Initialize the database
initialize_shadow_table(app.config['DATABASE'])
check_and_update_schema(db_path=app.config['DATABASE'])
initialize_tables(app.config['DATABASE'])

# Main routes start here
@app.route('/')
def index():
    """Main dashboard page"""
    return render_template('dashboard.html')

@app.route('/upload')
def upload_page():
    """Invoice upload page"""
    return render_template('upload.html')

@app.route('/batch-upload')
def batch_upload_page():
    """Display the batch upload page"""
    log.info("Redirecting from batch-upload to simple-batch-upload")
    return redirect(url_for('simple_batch_upload'), code=302)

@app.route('/simple-batch-upload')
def simple_batch_upload():
    """Simple batch upload page"""
    return render_template('simple_batch_upload.html')

@app.route('/sequential-validate')
def sequential_validate():
    """Sequential validation page for batch uploads"""
    return render_template('sequential_validate.html')

@app.route('/multi-upload')
@app.route('/multiupload')
@app.route('/multiple-upload')
@app.route('/multiple_upload')
@app.route('/multi_upload')
def multi_upload_page():
    """Alternative routes for batch invoice upload page"""
    return redirect(url_for('batch_upload_page'))

@app.route('/email-import')
def email_import_page():
    """Email-based invoice import page"""
    return render_template('email_import.html')

@app.route('/browser')
def browser_page():
    """Document browser page"""
    return render_template('browser.html')

@app.route('/lexoffice-import')
def lexoffice_import_page():
    """Render the LexOffice import page"""
    return render_template('lexoffice_import.html')

@app.route('/file-management')
def file_management_page():
    """File management page"""
    return render_template('file_management.html')

@app.route('/api/upload', methods=['POST'])
def upload_file():
    """API endpoint for file upload"""
    # Check if we're receiving a temp file path from validation step
    temp_file_path = request.form.get('temp_file_path')
    original_filename = request.form.get('original_filename')
    
    if temp_file_path:
        # Using pre-validated file from temporary storage
        if not os.path.exists(temp_file_path):
            return jsonify({
                'success': False,
                'error': 'Temporary file not found. Please try uploading again.'
            }), 404
            
        # Create a FileStorage-like object to use with existing process_invoice_file function
        # For this we need to read the file contents and create a file-like object
        try:
            with open(temp_file_path, 'rb') as f:
                file_contents = f.read()
                
            class TempFileStorage:
                def __init__(self, filename, file_contents):
                    self.filename = filename
                    self.file_contents = file_contents
                    self._file = io.BytesIO(file_contents)
                
                def save(self, path):
                    with open(path, 'wb') as f:
                        f._file = self._file
                        self._file.seek(0)
                        f.write(self.file_contents)
                        
                def read(self):
                    self._file.seek(0)
                    return self._file.read()
            
            # Create file storage object from temp file
            file = TempFileStorage(original_filename or os.path.basename(temp_file_path), file_contents)
            
            # Since this is coming from validation step, we don't need to check for duplicates
            # We'll do a final check before saving to database just to be safe
            skip_duplicate_check = True
            
            # We're going to extract the invoice data first to get the invoice number
            scanner = InvoiceScanner(app.config['DATABASE'], app.config['ARCHIVE_DIR'])
            extracted_data = scanner.extract_invoice_data(temp_file_path, store_in_db=False)
            
            # Make a quick check for duplicates before proceeding
            invoice_number = None
            if 'invoice_number' in extracted_data:
                invoice_number = extracted_data['invoice_number']
            elif 'Rechnungsnummer' in extracted_data:
                invoice_number = extracted_data['Rechnungsnummer']
                
            if invoice_number and invoice_number not in ['Unknown', 'Not found', 'Nicht gefunden', '']:
                # Check if this invoice number exists
                exists = check_invoice_exists(invoice_number, app.config['DATABASE'])
                if exists:
                    log.info(f"Duplicate invoice caught during final validation: {invoice_number}")
                    
                    # Clean up the temp file
                    if os.path.exists(temp_file_path):
                        try:
                            os.remove(temp_file_path)
                        except:
                            pass
                            
                    return jsonify({
                        'success': False,
                        'is_duplicate': True,
                        'invoice_number': invoice_number,
                        'error': f"Invoice number {invoice_number} already exists in the database"
                    }), 409
            
        except Exception as e:
            log.error(f"Error reading temporary file {temp_file_path}: {str(e)}")
            return jsonify({
                'success': False,
                'error': f'Error reading temporary file: {str(e)}'
            }), 500
    else:
        # Traditional file upload (check for file in request)
        if 'file' not in request.files:
            return jsonify({
                'success': False,
                'error': 'No file provided'
            }), 400
        
        file = request.files['file']
        
        if file.filename == '':
            return jsonify({
                'success': False,
                'error': 'No file selected'
            }), 400
        
        # For direct uploads, we need to check for duplicates
        skip_duplicate_check = False
    
    try:
        # Use the centralized invoice processor from utils/processing
        result = process_invoice_file(
            file, 
            app.config, 
            lambda: get_db_connection(app.config['DATABASE']),
            # Only check for duplicates if not using pre-validated file
            lambda file_path: {} if skip_duplicate_check else check_for_duplicate_invoice(
                file_path, 
                lambda x: check_invoice_exists(x, app.config['DATABASE']),
                lambda file_path, **kwargs: InvoiceScanner(app.config['DATABASE'], app.config['ARCHIVE_DIR']).extract_invoice_data(file_path, store_in_db=False)
            ), 
            select_ai_model,
            lambda data, batch_id=None, source_info=None: save_to_pending(data, batch_id, source_info, db_path=app.config['DATABASE']),
            InvoiceScanner,
            source='single_upload'
        )
        
        # Clean up temp file if used
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
                log.info(f"Removed temporary file after processing: {temp_file_path}")
            except Exception as e:
                log.warning(f"Could not remove temp file: {str(e)}")
        
        if result['success']:
            # Format response for frontend consumption
            response_data = {
                'success': True,
                'message': 'File uploaded and processed successfully - please validate data before finalizing',
                'file_path': result['file_path'],
                'preview_path': result.get('preview_path', ''),
                'pending_id': result['pending_id']
            }
            
            # Make sure extracted data is included
            if 'extracted_data' in result:
                response_data['extracted_data'] = result['extracted_data']
            
            # For compatibility, if not in result but in the form_data
            if 'extracted_data' not in response_data and result.get('form_data'):
                response_data['extracted_data'] = result.get('form_data')
            
            return jsonify(response_data)
        else:
            # Handle duplicate check
            if result.get('is_duplicate', False):
                return jsonify({
                    'success': False,
                    'error': result.get('error', 'This invoice appears to be a duplicate'),
                    'is_duplicate': True,
                    'file_path': result.get('file_path', ''),
                    'preview_path': result.get('preview_path', '')
                }), 409
            
            # Handle other errors
            status_code = 500
            cleanup_uploaded_files(result.get('file_path', ''), keep_preview=False)
            
            return jsonify({
                'success': False,
                'error': result.get('error', 'Error processing file')
            }), status_code
    
    except Exception as e:
        log.error(f"Error in upload_file: {str(e)}", exc_info=True)
        # Cleanup any saved files
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
            except:
                pass
        elif 'file' in locals() and hasattr(file, 'filename'):
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], sanitize_filename(file.filename))
            cleanup_uploaded_files(file_path, keep_preview=False)
            
        return jsonify({
            'success': False,
            'error': f'Error uploading file: {str(e)}'
        }), 500

@app.route('/api/dashboard-stats')
def get_dashboard_stats():
    """Get dashboard statistics"""
    conn = get_db_connection(app.config['DATABASE'])
    try:
        # Get total number of invoices
        cursor = conn.execute('SELECT COUNT(*) AS total FROM invoices')
        total_invoices = cursor.fetchone()['total']
        
        # Get total number of pending invoices
        cursor = conn.execute('SELECT COUNT(*) AS total FROM pending_invoices')
        total_pending = cursor.fetchone()['total']
        
        # Get total number of suppliers (using JOIN with suppliers table)
        cursor = conn.execute('SELECT COUNT(DISTINCT supplier_id) AS total FROM invoices')
        total_suppliers = cursor.fetchone()['total']
        
        # Get total number of companies (using JOIN with companies table)
        cursor = conn.execute('SELECT COUNT(DISTINCT company_id) AS total FROM invoices')
        total_companies = cursor.fetchone()['total']
        
        # Get total amount of all invoices
        cursor = conn.execute('SELECT SUM(amount) AS total_amount FROM invoices')
        total_amount = cursor.fetchone()['total_amount'] or 0
        
        # Get average amount
        cursor = conn.execute('SELECT AVG(amount) AS avg_amount FROM invoices')
        average_amount = cursor.fetchone()['avg_amount'] or 0
        
        # Get count of invoices needing manual input
        cursor = conn.execute('SELECT COUNT(*) AS total FROM pending_invoices WHERE needs_manual_input = 1')
        needs_manual = cursor.fetchone()['total']
        
        # Get recent uploads (processed today)
        today = datetime.now().strftime('%Y-%m-%d')
        cursor = conn.execute('SELECT COUNT(*) AS total FROM invoices WHERE processed_at LIKE ?', (f'{today}%',))
        recent_uploads = cursor.fetchone()['total']
        
        # Return stats
        return jsonify({
            'success': True,
            'stats': {
                'total_invoices': total_invoices,
                'total_pending': total_pending,
                'total_suppliers': total_suppliers,
                'total_companies': total_companies,
                'total_amount': total_amount,
                'average_amount': average_amount,
                'needs_manual_input': needs_manual,
                'recent_uploads': recent_uploads
            }
        })
    except Exception as e:
        log.error(f"Error getting dashboard stats: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/files')
def get_files():
    """Get all files in the system"""
    conn = get_db_connection(app.config['DATABASE'])
    try:
        # Use JOIN to get supplier and company names from their respective tables
        cursor = conn.execute('''
            SELECT i.id, i.file_path, i.original_path, 
                   s.name AS supplier_name,
                   i.invoice_number, i.invoice_date, 
                   i.amount, 
                   c.name AS company_name, 
                   i.description, i.processed_at AS created_at, i.processed_at AS updated_at
            FROM invoices i
            LEFT JOIN suppliers s ON i.supplier_id = s.id
            LEFT JOIN companies c ON i.company_id = c.id
            ORDER BY i.processed_at DESC
        ''')
        files = [dict(row) for row in cursor.fetchall()]
        return jsonify({'success': True, 'files': files})
    except Exception as e:
        log.error(f"Error getting files: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/companies')
def get_companies():
    """Get all companies"""
    conn = get_db_connection(app.config['DATABASE'])
    try:
        cursor = conn.execute('SELECT name FROM companies ORDER BY name')
        companies = [row['name'] for row in cursor.fetchall() if row['name']]
        return jsonify({'success': True, 'companies': companies})
    except Exception as e:
        log.error(f"Error getting companies: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/suppliers')
def get_suppliers():
    """Get all suppliers"""
    conn = get_db_connection(app.config['DATABASE'])
    try:
        cursor = conn.execute('SELECT name FROM suppliers ORDER BY name')
        suppliers = [row['name'] for row in cursor.fetchall() if row['name']]
        return jsonify({'success': True, 'suppliers': suppliers})
    except Exception as e:
        log.error(f"Error getting suppliers: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/invoices')
def get_invoices():
    """Get all invoices, with optional filtering"""
    conn = get_db_connection(app.config['DATABASE'])
    try:
        # Get query parameters
        status = request.args.get('status', 'all')
        supplier = request.args.get('supplier', '')
        company = request.args.get('company', '')
        date_from = request.args.get('date_from', '')
        date_to = request.args.get('date_to', '')
        invoice_id = request.args.get('id', '')
        needs_manual = request.args.get('needs_manual', '').lower() == 'true'
        
        # Build query
        query = ''
        params = []
        
        if status == 'pending':
            query = '''
                SELECT id, file_path, original_path, supplier_name, invoice_number, invoice_date, 
                      amount, company_name, description, created_at, updated_at, needs_manual_input
                FROM pending_invoices 
                WHERE 1=1
            '''
            
            # Add filters for pending invoices
            if supplier:
                query += ' AND supplier_name LIKE ?'
                params.append(f'%{supplier}%')
            if company:
                query += ' AND company_name LIKE ?'
                params.append(f'%{company}%')
            if date_from:
                query += ' AND invoice_date >= ?'
                params.append(date_from)
            if date_to:
                query += ' AND invoice_date <= ?'
                params.append(date_to)
            if needs_manual:
                query += ' AND (needs_manual_input = 1 OR needs_manual_input = "true")'
            if invoice_id:
                query += ' AND id = ?'
                params.append(invoice_id)
                
            # Add order by
            query += ' ORDER BY created_at DESC'
        else:
            query = '''
                SELECT i.id, i.file_path, i.original_path, 
                       s.name AS supplier_name,
                       i.invoice_number, i.invoice_date, 
                       i.amount, 
                       c.name AS company_name, 
                       i.description, i.processed_at AS created_at, i.processed_at AS updated_at
                FROM invoices i
                LEFT JOIN suppliers s ON i.supplier_id = s.id
                LEFT JOIN companies c ON i.company_id = c.id
                WHERE 1=1
            '''
            
            # Add filters for regular invoices
            if supplier:
                query += ' AND s.name LIKE ?'
                params.append(f'%{supplier}%')
            if company:
                query += ' AND c.name LIKE ?'
                params.append(f'%{company}%')
            if date_from:
                query += ' AND i.invoice_date >= ?'
                params.append(date_from)
            if date_to:
                query += ' AND i.invoice_date <= ?'
                params.append(date_to)
            if invoice_id:
                query += ' AND i.id = ?'
                params.append(invoice_id)
                
            # Add order by
            query += ' ORDER BY i.processed_at DESC'
        
        # Execute query
        cursor = conn.execute(query, params)
        invoices = [dict(row) for row in cursor.fetchall()]
        
        return jsonify({'success': True, 'invoices': invoices})
    except Exception as e:
        log.error(f"Error getting invoices: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/pending-invoices')
def get_pending_invoices():
    """Get pending invoices with optional filtering"""
    conn = get_db_connection(app.config['DATABASE'])
    try:
        # Get query parameters
        batch_id = request.args.get('batch_id', '')
        needs_manual = request.args.get('needs_manual', '').lower() == 'true'
        
        # Build query
        query = '''
            SELECT id, batch_id, file_path, preview_path, supplier_name, invoice_number, 
                   invoice_date, amount, company_name, description, needs_manual_input,
                   validation_status, created_at, updated_at
            FROM pending_invoices 
            WHERE 1=1
        '''
        params = []
        
        if batch_id:
            query += ' AND batch_id = ?'
            params.append(batch_id)
        
        if needs_manual:
            query += ' AND (needs_manual_input = 1 OR needs_manual_input = "true")'
            
        # Add order by
        query += ' ORDER BY created_at DESC'
        
        # Execute query
        cursor = conn.execute(query, params)
        pending = [dict(row) for row in cursor.fetchall()]
        
        return jsonify({'success': True, 'pending_invoices': pending})
    except Exception as e:
        log.error(f"Error getting pending invoices: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/pending/<int:pending_id>', methods=['GET'])
def get_pending_invoice(pending_id):
    """Get a specific pending invoice"""
    conn = get_db_connection(app.config['DATABASE'])
    try:
        cursor = conn.execute('SELECT * FROM pending_invoices WHERE id = ?', (pending_id,))
        invoice = cursor.fetchone()
        
        if not invoice:
            return jsonify({'success': False, 'error': 'Invoice not found'}), 404
            
        return jsonify({'success': True, 'invoice': dict(invoice)})
    except Exception as e:
        log.error(f"Error getting pending invoice: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/pending/<int:pending_id>', methods=['PUT'])
def update_pending_invoice_route(pending_id):
    """Update a pending invoice"""
    try:
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
            
        result = update_pending_invoice(pending_id, data, app.config['DATABASE'])
        
        if result:
            return jsonify({'success': True, 'message': 'Invoice updated successfully'})
        else:
            return jsonify({'success': False, 'error': 'Failed to update invoice'}), 500
    except Exception as e:
        log.error(f"Error updating pending invoice: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/validate/<int:pending_id>', methods=['POST'])
def validate_invoice(pending_id):
    """Validate a pending invoice and move to the main invoices table"""
    conn = get_db_connection(app.config['DATABASE'])
    try:
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
        
        # First update the pending invoice with validation data
        update_data = {
            'supplier_name': data.get('supplier_name', ''),
            'company_name': data.get('company_name', ''),
            'invoice_number': data.get('invoice_number', ''),
            'invoice_date': data.get('invoice_date', ''),
            'amount': data.get('amount', ''),
            'vat_amount': data.get('vat_amount', ''),
            'description': data.get('description', ''),
            'is_validated': True,
            'validated_at': datetime.now().isoformat(),
            'validation_status': 'human_validated'
        }
        
        # Update the pending invoice
        update_result = update_pending_invoice(pending_id, update_data, app.config['DATABASE'])
        
        if not update_result:
            return jsonify({'success': False, 'error': 'Failed to update pending invoice'}), 500
        
        # Get the updated pending invoice
        cursor = conn.execute('SELECT * FROM pending_invoices WHERE id = ?', (pending_id,))
        pending = cursor.fetchone()
        
        if not pending:
            return jsonify({'success': False, 'error': 'Invoice not found after update'}), 404
        
        # Check for duplicate invoice number BEFORE database insertion
        invoice_number = data.get('invoice_number') or pending['invoice_number']
        if invoice_number:
            cursor = conn.execute('SELECT id FROM invoices WHERE invoice_number = ?', (invoice_number,))
            existing = cursor.fetchone()
            
            if existing:
                return jsonify({
                    'success': False,
                    'error': f'Invoice number {invoice_number} already exists in database',
                    'is_duplicate': True,
                    'invoice_number': invoice_number
                }), 409
        
        # Get or create supplier and company
        supplier_id = None
        supplier_name = data.get('supplier_name') or pending['supplier_name']
        cursor = conn.execute('SELECT id FROM suppliers WHERE name = ?', (supplier_name,))
        supplier = cursor.fetchone()
        if supplier:
            supplier_id = supplier['id']
        else:
            cursor.execute('INSERT INTO suppliers (name) VALUES (?)', (supplier_name,))
            supplier_id = cursor.lastrowid
        
        company_id = None
        company_name = data.get('company_name') or pending['company_name']
        cursor = conn.execute('SELECT id FROM companies WHERE name = ?', (company_name,))
        company = cursor.fetchone()
        if company:
            company_id = company['id']
        else:
            cursor.execute('INSERT INTO companies (name) VALUES (?)', (company_name,))
            company_id = cursor.lastrowid
        
        # Format the amount and VAT as floats if they're not already
        amount_str = data.get('amount') or pending['amount']
        amount = 0.0
        if amount_str:
            try:
                amount = normalize_amount(amount_str)
            except:
                amount = 0.0
                
        vat_amount_str = data.get('vat_amount') or pending['vat_amount']
        vat_amount = 0.0
        if vat_amount_str:
            try:
                vat_amount = normalize_amount(vat_amount_str)
            except:
                vat_amount = 0.0
        
        # Get file paths
        file_path = pending['file_path']
        original_path = pending['original_path']
        
        # Check if this is a temp file that needs cleanup after processing
        temp_file_to_clean = None
        if file_path and app.config['TEMP_FOLDER'] in file_path:
            temp_file_to_clean = file_path
        
        # Now insert into invoices table
        cursor.execute('''
            INSERT INTO invoices (
                file_path, original_path, invoice_number, invoice_date, 
                amount, amount_original, vat_amount, vat_amount_original,
                description, supplier_id, company_id, processed_at, source_info
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            file_path,
            original_path,
            invoice_number,
            data.get('invoice_date') or pending['invoice_date'],
            amount,
            amount_str,
            vat_amount,
            vat_amount_str,
            data.get('description') or pending['description'],
            supplier_id,
            company_id,
            datetime.now().isoformat(),
            'Validated by human'
        ))
        
        # Get the ID of the new invoice
        invoice_id = cursor.lastrowid
        
        # Mark pending as finalized
        cursor.execute('''
            UPDATE pending_invoices 
            SET is_finalized = 1, finalized_at = ? 
            WHERE id = ?
        ''', (datetime.now().isoformat(), pending_id))
        
        # Commit the transaction
        conn.commit()
        
        # If there's a batch_id, update the batch queue
        if pending['batch_id']:
            try:
                cursor.execute('''
                    UPDATE batch_queue 
                    SET status = 'processed' 
                    WHERE batch_id = ? AND pending_id = ?
                ''', (pending['batch_id'], pending_id))
                conn.commit()
            except Exception as batch_e:
                log.warning(f"Error updating batch status: {str(batch_e)}")
                # Don't fail the whole operation if just the batch update fails
        
        # Organize the file if requested
        if data.get('organize_file', True):
            try:
                # Get archive directory
                archive_path = organize_file(file_path, app.config['ARCHIVE_DIR'])
                
                if archive_path:
                    # Update the invoice with the new file path
                    cursor.execute('''
                        UPDATE invoices 
                        SET file_path = ? 
                        WHERE id = ?
                    ''', (archive_path, invoice_id))
                    conn.commit()
                    
                    # Clean up the original file if not in temp directory (temp files are handled separately)
                    if not temp_file_to_clean:
                        cleanup_processed_files(
                            file_path, 
                            archive_path,
                            app.config['UPLOAD_FOLDER'],
                            app.config['PREVIEW_FOLDER']
                        )
            except Exception as org_e:
                log.warning(f"Error organizing file: {str(org_e)}")
                # Don't fail if just the file organization fails
        
        # Clean up temp file if it exists
        if temp_file_to_clean and os.path.exists(temp_file_to_clean):
            try:
                os.remove(temp_file_to_clean)
                log.info(f"Cleaned up temporary file after successful validation: {temp_file_to_clean}")
            except Exception as e:
                log.warning(f"Error cleaning up temporary file {temp_file_to_clean}: {str(e)}")
        
        return jsonify({
            'success': True, 
            'message': 'Invoice validated and finalized',
            'invoice_id': invoice_id
        })
                
    except Exception as e:
        conn.rollback()
        log.error(f"Error validating invoice: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/batch/create', methods=['POST'])
def create_batch():
    """Create a new batch for multiple uploads"""
    try:
        # Generate a unique batch ID
        batch_id = str(uuid.uuid4())
        
        return jsonify({
            'success': True,
            'batch_id': batch_id,
            'created_at': datetime.now().isoformat()
        })
    except Exception as e:
        log.error(f"Error creating batch: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/batch/<batch_id>/upload', methods=['POST'])
def batch_upload_file(batch_id):
    """Upload a file to a batch"""
    if 'file' not in request.files:
        return jsonify({
            'success': False, 
            'error': 'No file provided'
        }), 400
    
    file = request.files['file']
    position = request.form.get('position', 0)
    
    if file.filename == '':
        return jsonify({
            'success': False,
            'error': 'No file selected'
        }), 400
    
    conn = get_db_connection(app.config['DATABASE'])
    try:
        # Process the file
        result = process_invoice_file(
            file, 
            app.config,
            lambda: conn,
            lambda file_path: check_for_duplicate_invoice(
                file_path, 
                lambda x: check_invoice_exists(x, app.config['DATABASE']),
                lambda file_path, **kwargs: InvoiceScanner(app.config['DATABASE'], app.config['ARCHIVE_DIR']).extract_invoice_data(file_path)
            ),
            select_ai_model,
            lambda data, batch=None, source=None: save_to_pending(data, batch, source, db_path=app.config['DATABASE']),
            InvoiceScanner,
            batch_id=batch_id,
            source='batch_upload'
        )
        
        if result['success']:
            # Add to batch queue
            cursor = conn.execute('''
                INSERT INTO batch_queue (
                    batch_id, file_path, preview_path, filename, status, pending_id, position
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                batch_id,
                result['file_path'],
                result.get('preview_path', ''),
                file.filename,
                'pending',
                result['pending_id'],
                position
            ))
            conn.commit()
            
            queue_id = cursor.lastrowid
            
            return jsonify({
                'success': True,
                'message': 'File uploaded to batch successfully',
                'batch_id': batch_id,
                'queue_id': queue_id,
                'pending_id': result['pending_id'],
                'file_path': result['file_path'],
                'preview_path': result.get('preview_path', ''),
                'filename': file.filename,
                'position': position
            })
        else:
            # Check for duplicate
            if result.get('is_duplicate', False):
                return jsonify({
                    'success': False,
                    'error': result.get('error', 'This invoice appears to be a duplicate'),
                    'is_duplicate': True,
                    'filename': file.filename
                }), 409
            
            return jsonify({
                'success': False,
                'error': result.get('error', 'Error processing file'),
                'filename': file.filename
            }), 500
            
    except Exception as e:
        conn.rollback()
        log.error(f"Error in batch upload: {str(e)}")
        return jsonify({
            'success': False, 
            'error': str(e),
            'filename': file.filename
        }), 500
    finally:
        conn.close()

@app.route('/api/batch/<batch_id>/files', methods=['GET'])
def get_batch_files(batch_id):
    """Get all files in a batch"""
    conn = get_db_connection(app.config['DATABASE'])
    try:
        cursor = conn.execute('''
            SELECT batch_queue.*, pending_invoices.supplier_name, pending_invoices.invoice_number,
                   pending_invoices.invoice_date, pending_invoices.amount
            FROM batch_queue 
            LEFT JOIN pending_invoices ON batch_queue.pending_id = pending_invoices.id
            WHERE batch_queue.batch_id = ?
            ORDER BY batch_queue.position ASC
        ''', (batch_id,))
        
        files = [dict(row) for row in cursor.fetchall()]
        
        return jsonify({'success': True, 'files': files})
    except Exception as e:
        log.error(f"Error getting batch files: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/view-pdf/<path:filename>')
def view_pdf(filename):
    """View PDF file - used by frontend for preview"""
    try:
        # Decode the filename (it may be URL encoded)
        filename = filename.replace('\\', '/')
        
        # Handle Windows paths
        if filename.startswith('C:/') or filename.startswith('c:/'):
            # For Windows paths, make sure they're properly formatted
            log.info(f"Serving file from Windows path: {filename}")
        
        # Check if the file exists
        if os.path.isfile(filename):
            directory = os.path.dirname(filename)
            base_name = os.path.basename(filename)
            
            # Return the file
            return send_file(
                os.path.join(directory, base_name),
                mimetype='application/pdf',
                as_attachment=False
            )
        else:
            log.error(f"File not found: {filename}")
            # Return a more user-friendly message
            return jsonify({
                'success': False,
                'error': 'PDF file not found',
                'path': filename
            }), 404
    except Exception as e:
        log.error(f"Error viewing PDF: {str(e)}")
        return jsonify({
            'success': False,
            'error': f"Error viewing file: {str(e)}",
            'path': filename
        }), 500

@app.route('/api/lexoffice/credentials', methods=['GET'])
def get_lexoffice_credentials():
    """Get LexOffice API credentials"""
    # Simple mock response for now
    return jsonify({
        'success': True,
        'has_credentials': False
    })

@app.route('/api/lexoffice/sync-status', methods=['GET'])
def get_lexoffice_sync_status():
    """Get LexOffice sync status"""
    # Simple mock response for now
    return jsonify({
        'success': True,
        'last_sync': None,
        'sync_in_progress': False,
        'total_synced': 0
    })

# Email-related endpoints
@app.route('/api/email/connect', methods=['POST'])
def connect_email():
    """Connect to email server"""
    try:
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
            
        email_addr = data.get('email')
        password = data.get('password')
        imap_server = data.get('imap_server')
        port = data.get('port', 993)
        use_ssl = data.get('use_ssl', True)
        is_custom = imap_server == 'custom'
        custom_server = data.get('custom_server')
        save_account = data.get('save_account', False)
        
        if not email_addr or not password or not imap_server:
            return jsonify({
                'success': False,
                'error': 'Missing required fields: email, password, or server'
            }), 400
            
        # For custom server, validate additional parameters
        if is_custom and not custom_server:
            return jsonify({
                'success': False,
                'error': 'Custom server selected but no server address provided'
            }), 400
        
        # Clean up expired connections
        cleanup_expired_connections(app.config['EMAIL_SESSION_TIMEOUT'])
        
        # Set a longer timeout for connection attempts
        socket.setdefaulttimeout(30)
        
        # Connect to email server
        result = connect_to_email(
            email_addr, password, imap_server, port, use_ssl, custom_server
        )
        
        if isinstance(result, dict) and result.get('success'):
            # Save credentials if requested
            if save_account:
                try:
                    conn = get_db_connection(app.config['DATABASE'])
                    save_result, account_id = save_email_credentials(
                        email_addr, password, imap_server, port, use_ssl, is_custom, custom_server, conn
                    )
                    conn.close()
                
                    if save_result:
                        log.info(f"Saved email credentials for {email_addr}")
                        # Add account_id to the result
                        result['account_id'] = account_id
                    else:
                        log.warning(f"Failed to save email credentials for {email_addr}")
                except Exception as save_err:
                    log.error(f"Error saving credentials: {str(save_err)}")
            
            return jsonify(result)
        elif isinstance(result, dict):
            # It's an error dict but not a tuple
            return jsonify(result), 500
        else:
            # It's a tuple with (result, status_code)
            return jsonify(result[0]), result[1]
    except ConnectionRefusedError as conn_err:
        log.error(f"Connection refused: {str(conn_err)}")
        return jsonify({
            'success': False,
            'error': 'Connection refused. Please check server address and port.'
        }), 503
    except socket.timeout:
        log.error("Connection timed out")
        return jsonify({
            'success': False,
            'error': 'Connection timed out. Please check your server settings and try again.'
        }), 504
    except socket.error as sock_err:
        log.error(f"Socket error: {str(sock_err)}")
        return jsonify({
            'success': False,
            'error': f'Connection error: {str(sock_err)}'
        }), 503
    except imaplib.IMAP4.error as imap_err:
        log.error(f"IMAP error: {str(imap_err)}")
        error_msg = str(imap_err).lower()
        if "login" in error_msg or "auth" in error_msg:
            return jsonify({
                'success': False,
                'error': 'Authentication failed. Please check your email and password.'
            }), 401
        else:
            return jsonify({
                'success': False,
                'error': f'IMAP server error: {str(imap_err)}'
            }), 500
    except Exception as e:
        log.error(f"Error connecting to email: {str(e)}")
        return jsonify({
            'success': False,
            'error': f'Error connecting to email: {str(e)}'
        }), 500

@app.route('/api/email/disconnect', methods=['POST'])
def disconnect_email_session():
    """Disconnect from email server"""
    try:
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
            
        session_id = data.get('session_id')
        if not session_id:
            return jsonify({'success': False, 'error': 'No session ID provided'}), 400
        
        result = disconnect_email(session_id)
        return jsonify(result)
    except Exception as e:
        log.error(f"Error disconnecting email: {str(e)}")
        return jsonify({
            'success': False,
            'error': f'Error disconnecting email: {str(e)}'
        }), 500

@app.route('/api/email/mailboxes/<session_id>', methods=['GET'])
def get_mailboxes(session_id):
    """Get available mailboxes"""
    if session_id not in email_connections:
        return jsonify({
            'success': False,
            'error': 'Email session not found or expired'
        }), 404
        
    try:
        mail = email_connections[session_id]['connection']
        
        # List available mailboxes
        response, mailboxes = mail.list()
        
        if response != 'OK':
            return jsonify({
                'success': False,
                'error': f'Failed to retrieve mailboxes: {response}'
            }), 500
            
        # Parse mailboxes
        mailbox_list = []
        for mailbox in mailboxes:
            try:
                # Decode the mailbox name
                parts = mailbox.decode().split(' "." ')
                if len(parts) > 1:
                    # Extract the mailbox name from the response
                    mailbox_name = parts[1].strip('"')
                    mailbox_list.append(mailbox_name)
            except Exception as mb_e:
                log.warning(f"Error parsing mailbox: {str(mb_e)}")
        
        return jsonify({
            'success': True,
            'mailboxes': mailbox_list
        })
    except Exception as e:
        log.error(f"Error getting mailboxes: {str(e)}")
        return jsonify({
            'success': False,
            'error': f'Error getting mailboxes: {str(e)}'
        }), 500

# Email account management routes
@app.route('/api/email/accounts', methods=['GET'])
def get_email_accounts():
    """Get saved email accounts"""
    try:
        conn = get_db_connection(app.config['DATABASE'])
        accounts = get_email_credentials(db_connection=conn)
        conn.close()
        
        if accounts is not None:
            return jsonify({
                'success': True,
                'accounts': accounts
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Failed to retrieve email accounts'
            }), 500
    except Exception as e:
        log.error(f"Error getting email accounts: {str(e)}")
        return jsonify({
            'success': False,
            'error': f'Error getting email accounts: {str(e)}'
        }), 500

@app.route('/api/email/account/<int:account_id>', methods=['GET'])
def get_email_account(account_id):
    """Get a specific email account with credentials"""
    try:
        conn = get_db_connection(app.config['DATABASE'])
        account = get_email_credentials(credential_id=account_id, db_connection=conn)
        conn.close()
        
        if account is not None:
            return jsonify({
                'success': True,
                'account': account
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Email account not found'
            }), 404
    except Exception as e:
        log.error(f"Error getting email account: {str(e)}")
        return jsonify({
            'success': False,
            'error': f'Error getting email account: {str(e)}'
        }), 500

@app.route('/api/email/account/<int:account_id>', methods=['DELETE'])
def delete_email_account(account_id):
    """Delete a saved email account"""
    try:
        conn = get_db_connection(app.config['DATABASE'])
        success = delete_email_credentials(credential_id=account_id, db_connection=conn)
        conn.close()
        
        if success:
            return jsonify({
                'success': True,
                'message': 'Email account deleted successfully'
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Failed to delete email account'
            }), 500
    except Exception as e:
        log.error(f"Error deleting email account: {str(e)}")
        return jsonify({
            'success': False,
            'error': f'Error deleting email account: {str(e)}'
        }), 500

# Email search and processing routes
@app.route('/api/email/search', methods=['POST'])
def search_emails():
    """Search for emails matching criteria"""
    try:
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
            
        session_id = data.get('session_id')
        criteria = data.get('criteria', '')
        max_emails = min(data.get('max_emails', 25), 50)  # Limit to max 50 emails
        offset = data.get('offset', 0)  # For pagination
        delay_between_fetches = max(data.get('delay', 3), 3)  # Minimum 3 second delay
        
        if session_id not in email_connections:
            return jsonify({
                'success': False,
                'error': 'Email session not found or expired',
                'reconnect_required': True
            }), 404
        
        mail = email_connections[session_id]['connection']
        
        # Update the last activity timestamp
        email_connections[session_id]['last_activity'] = datetime.now()
        
        # Select the inbox or another mailbox if specified
        mailbox = data.get('mailbox', 'INBOX')
        response, _ = mail.select(mailbox)
        
        if response != 'OK':
            return jsonify({
                'success': False,
                'error': f'Failed to select mailbox: {response}'
            }), 500
        
        # Store the selected mailbox
        email_connections[session_id]['selected_mailbox'] = mailbox
        
        # Set socket timeout for search operations - use a longer timeout
        socket.setdefaulttimeout(90)  # Increase timeout for search operations
        
        # Search for emails
        search_criteria = 'ALL'
        if criteria:
            # Use the provided criteria
            search_criteria = criteria
            
        # Add a default criteria to filter for emails with attachments and invoice-related subjects
        if search_criteria == 'ALL':
            # Look for emails with attachments that might contain invoices
            try:
                search_criteria = '(OR (SUBJECT "invoice") (SUBJECT "rechnung") (SUBJECT "bill") (SUBJECT "payment") (HEADER Content-Type "application/pdf"))'
                response, email_ids = mail.search(None, search_criteria)
            except Exception as e:
                # If complex search fails, fall back to ALL
                log.warning(f"Complex search failed, falling back to ALL: {str(e)}")
                search_criteria = 'ALL'
                response, email_ids = mail.search(None, search_criteria)
        else:
            response, email_ids = mail.search(None, search_criteria)
        
        if response != 'OK':
            return jsonify({
                'success': False,
                'error': f'Failed to search emails: {response}'
            }), 500
        
        # Get email IDs as a list
        email_id_list = email_ids[0].split() if email_ids and email_ids[0] else []
        
        # Get total count for pagination
        total_count = len(email_id_list)
        
        # Apply pagination - get a slice of email IDs
        if offset > 0:
            # If offset is provided, get emails from that point
            email_id_list = email_id_list[offset:offset+max_emails]
        else:
            # Otherwise get the most recent emails (from the end of the list)
            start_idx = max(0, len(email_id_list) - max_emails)
            email_id_list = email_id_list[start_idx:]
        
        # Fetch email headers for the IDs
        emails = []
        for e_id in email_id_list:
            try:
                # Add delay between fetches to avoid server throttling
                time.sleep(delay_between_fetches)
                
                # Fetch email header
                response, msg_data = mail.fetch(e_id, '(BODY.PEEK[HEADER] FLAGS)')
                
                if response != 'OK':
                    continue
                
                # Parse email header
                email_message = email.message_from_bytes(msg_data[0][1])
                
                # Check for attachments by fetching structure
                has_attachment = False
                try:
                    response, msg_struct = mail.fetch(e_id, '(BODYSTRUCTURE)')
                    if response == 'OK':
                        # Simple heuristic - if 'attachment' is in the structure string
                        struct_str = str(msg_struct[0]).lower()
                        has_attachment = 'attachment' in struct_str or 'application/pdf' in struct_str
                except Exception as struct_err:
                    log.warning(f"Error checking for attachments: {str(struct_err)}")
                    # Default to checking Content-Type header as fallback
                    has_attachment = 'multipart/mixed' in email_message.get('Content-Type', '').lower()
                
                # Get flags to determine if email has been read
                flags = ""
                for flag_data in msg_data:
                    if isinstance(flag_data, tuple) and b'FLAGS' in flag_data[0]:
                        flags = flag_data[0].decode()
                        break
                is_read = '\\Seen' in flags
                
                # Determine if the email might be invoice-related
                subject = decode_header(email_message.get('Subject', ''))[0][0]
                if isinstance(subject, bytes):
                    subject = subject.decode('utf-8', errors='replace')
                    
                from_header = decode_header(email_message.get('From', ''))[0][0]
                if isinstance(from_header, bytes):
                    from_header = from_header.decode('utf-8', errors='replace')
                    
                # Clean up the "From" field to extract name and email
                from_name = from_header
                from_email = from_header
                if '<' in from_header and '>' in from_header:
                    parts = from_header.split('<')
                    from_name = parts[0].strip()
                    from_email = parts[1].split('>')[0].strip()
                
                # Check if the email is likely invoice-related
                invoice_keywords = ['invoice', 'rechnung', 'bill', 'payment', 'faktura', 'facture']
                is_invoice_related = any(keyword in subject.lower() for keyword in invoice_keywords)
                
                # Parse date
                date_header = email_message.get('Date', '')
                try:
                    parsed_date = email.utils.parsedate_to_datetime(date_header)
                    formatted_date = parsed_date.strftime('%Y-%m-%d %H:%M:%S')
                except:
                    formatted_date = date_header
                
                # Add email metadata to the list
                emails.append({
                    'id': e_id.decode(),
                    'subject': subject,
                    'from': from_name,
                    'from_email': from_email,
                    'date': formatted_date,
                    'is_read': is_read,
                    'hasAttachment': has_attachment,
                    'isInvoiceRelated': is_invoice_related
                })
            except Exception as e:
                log.error(f"Error parsing email {e_id}: {str(e)}")
                continue
        
        return jsonify({
            'success': True,
            'emails': emails,
            'total_count': total_count,
            'fetched_count': len(emails),
            'offset': offset,
            'has_more': offset + len(emails) < total_count
        })
    except Exception as e:
        log.error(f"Error searching emails: {str(e)}")
        return jsonify({
            'success': False,
            'error': f'Error searching emails: {str(e)}'
        }), 500

@app.route('/api/email/fetch', methods=['POST'])
def fetch_email():
    """Fetch a specific email with its content and attachments"""
    try:
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
            
        session_id = data.get('session_id')
        email_id = data.get('email_id')
        delay = max(data.get('delay', 3), 3)  # Minimum 3 second delay
        
        if not session_id or not email_id:
            return jsonify({
                'success': False,
                'error': 'Missing session_id or email_id'
            }), 400
            
        if session_id not in email_connections:
            return jsonify({
                'success': False,
                'error': 'Email session not found or expired',
                'reconnect_required': True
            }), 404
        
        mail = email_connections[session_id]['connection']
        
        # Update the last activity timestamp
        email_connections[session_id]['last_activity'] = datetime.now()
        
        # Make sure a mailbox is selected
        if not email_connections[session_id].get('selected_mailbox'):
            try:
                response, _ = mail.select('INBOX')
                if response != 'OK':
                    return jsonify({
                        'success': False,
                        'error': 'Failed to select mailbox',
                        'reconnect_required': True
                    }), 500
                email_connections[session_id]['selected_mailbox'] = 'INBOX'
            except Exception as select_err:
                log.error(f"Error selecting mailbox: {str(select_err)}")
                return jsonify({
                    'success': False,
                    'error': f'Error selecting mailbox: {str(select_err)}',
                    'reconnect_required': True
                }), 500
        
        # Set longer timeout for fetch operations
        socket.setdefaulttimeout(90)
        
        # Fetch email content with a delay to avoid server throttling
        time.sleep(delay)
        
        try:
            response, msg_data = mail.fetch(email_id.encode(), '(RFC822)')
            
            if response != 'OK':
                return jsonify({
                    'success': False,
                    'error': f'Failed to fetch email: {response}',
                    'reconnect_required': response.lower() == 'no' or 'timeout' in response.lower()
                }), 500
            
            # Parse email content
            email_message = email.message_from_bytes(msg_data[0][1])
            
            # Extract basic email info
            subject = decode_header(email_message.get('Subject', ''))[0][0]
            if isinstance(subject, bytes):
                subject = subject.decode('utf-8', errors='replace')
                
            from_header = decode_header(email_message.get('From', ''))[0][0]
            if isinstance(from_header, bytes):
                from_header = from_header.decode('utf-8', errors='replace')
                
            # Parse date
            date_header = email_message.get('Date', '')
            try:
                parsed_date = email.utils.parsedate_to_datetime(date_header)
                formatted_date = parsed_date.strftime('%Y-%m-%d %H:%M:%S')
            except:
                formatted_date = date_header
            
            # Extract email body
            body = ""
            html_body = ""
            attachments = []
            
            # Process email parts
            for part in email_message.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get('Content-Disposition'))
                
                # Get the email body
                if content_type == 'text/plain' and 'attachment' not in content_disposition:
                    try:
                        body_bytes = part.get_payload(decode=True)
                        charset = part.get_content_charset() or 'utf-8'
                        body = body_bytes.decode(charset, errors='replace')
                    except Exception as e:
                        log.error(f"Error decoding email body: {str(e)}")
                        body = "Error decoding email body"
                
                # Get HTML body
                elif content_type == 'text/html' and 'attachment' not in content_disposition:
                    try:
                        html_bytes = part.get_payload(decode=True)
                        charset = part.get_content_charset() or 'utf-8'
                        html_body = html_bytes.decode(charset, errors='replace')
                    except Exception as e:
                        log.error(f"Error decoding HTML body: {str(e)}")
                        html_body = "<p>Error decoding HTML body</p>"
                
                # Get attachments
                elif 'attachment' in content_disposition or content_type.startswith('application/'):
                    filename = part.get_filename()
                    if filename:
                        # Try to decode the filename
                        filename_parts = decode_header(filename)
                        if filename_parts and filename_parts[0]:
                            if isinstance(filename_parts[0][0], bytes):
                                try:
                                    charset = filename_parts[0][1] or 'utf-8'
                                    filename = filename_parts[0][0].decode(charset, errors='replace')
                                except:
                                    filename = filename_parts[0][0].decode('utf-8', errors='replace')
                        
                        # Sanitize filename
                        filename = re.sub(r'[\\/*?:"<>|]', '_', filename)
                        
                        # Add to attachments list with more useful metadata
                        mime_type = content_type
                        file_ext = os.path.splitext(filename)[1].lower()
                        is_invoice = file_ext == '.pdf' or 'invoice' in filename.lower() or 'rechnung' in filename.lower()
                        
                        attachments.append({
                            'id': str(len(attachments) + 1),
                            'filename': filename,
                            'mime_type': mime_type,
                            'size': len(part.get_payload(decode=True)),
                            'is_invoice': is_invoice
                        })
            
            # Format the response
            email_data = {
                'id': email_id,
                'subject': subject,
                'from': from_header,
                'date': formatted_date,
                'body': body,
                'html_body': html_body,
                'attachments': attachments
            }
            
            return jsonify({
                'success': True,
                'email': email_data
            })
            
        except Exception as fetch_error:
            log.error(f"Error during mail fetch: {str(fetch_error)}")
            
            # Determine if we need to reconnect based on the error
            reconnect_required = isinstance(fetch_error, imaplib.IMAP4.error) or isinstance(fetch_error, socket.error)
            error_str = str(fetch_error).lower()
            reconnect_required = reconnect_required or 'timeout' in error_str or 'connection' in error_str
            
            return jsonify({
                'success': False,
                'error': f'Error fetching email: {str(fetch_error)}',
                'reconnect_required': reconnect_required
            }), 500
    except Exception as e:
        log.error(f"Error fetching email: {str(e)}")
        
        # Determine if we need to reconnect based on the error
        reconnect_required = isinstance(e, imaplib.IMAP4.error) or isinstance(e, socket.error)
        error_str = str(e).lower()
        reconnect_required = reconnect_required or 'timeout' in error_str or 'connection' in error_str
        
        return jsonify({
            'success': False,
            'error': f'Error fetching email: {str(e)}',
            'reconnect_required': reconnect_required
        }), 500

@app.route('/api/email/download-attachment', methods=['POST'])
def download_attachment():
    """Download an attachment from an email"""
    try:
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
            
        session_id = data.get('session_id')
        email_id = data.get('email_id')
        attachment_id = data.get('attachment_id')
        delay = data.get('delay', 1.0)  # Add delay parameter with higher default
        
        if not session_id or not email_id or not attachment_id:
            return jsonify({
                'success': False,
                'error': 'Missing required parameters'
            }), 400
            
        if session_id not in email_connections:
            return jsonify({
                'success': False,
                'error': 'Email session not found or expired'
            }), 404
        
        mail = email_connections[session_id]['connection']
        
        # Make sure a mailbox is selected
        if not email_connections[session_id].get('selected_mailbox'):
            response, _ = mail.select('INBOX')
            if response != 'OK':
                return jsonify({
                    'success': False,
                    'error': 'Failed to select mailbox'
                }), 500
            email_connections[session_id]['selected_mailbox'] = 'INBOX'
        
        # Add delay before fetching to avoid server throttling
        time.sleep(delay)
        
        # Fetch email content
        response, msg_data = mail.fetch(email_id.encode(), '(RFC822)')
        
        if response != 'OK':
            return jsonify({
                'success': False,
                'error': f'Failed to fetch email: {response}'
            }), 500
        
        # Parse email content
        email_message = email.message_from_bytes(msg_data[0][1])
        
        # Extract email information for the attachment metadata
        subject = decode_header(email_message.get('Subject', ''))[0][0]
        if isinstance(subject, bytes):
            subject = subject.decode('utf-8', errors='replace')
            
        from_header = decode_header(email_message.get('From', ''))[0][0]
        if isinstance(from_header, bytes):
            from_header = from_header.decode('utf-8', errors='replace')
        
        # Find the specified attachment
        attachment_index = 0
        attachment_found = False
        attachment_filename = None
        attachment_content = None
        attachment_mime_type = None
        
        for part in email_message.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get('Content-Disposition'))
            
            # Skip non-attachment parts
            if 'attachment' not in content_disposition and not content_type.startswith('application/'):
                continue
            
            filename = part.get_filename()
            if not filename:
                attachment_index += 1
                continue
            
            # Try to decode the filename if needed
            filename_parts = decode_header(filename)
            if filename_parts and filename_parts[0]:
                if isinstance(filename_parts[0][0], bytes):
                    try:
                        charset = filename_parts[0][1] or 'utf-8'
                        filename = filename_parts[0][0].decode(charset, errors='replace')
                    except:
                        filename = filename_parts[0][0].decode('utf-8', errors='replace')
            
            # Sanitize filename
            filename = re.sub(r'[\\/*?:"<>|]', '_', filename)
            
            attachment_index += 1
            
            # Check if this is the attachment we're looking for
            if str(attachment_index) == attachment_id:
                attachment_found = True
                attachment_filename = filename
                attachment_content = part.get_payload(decode=True)
                attachment_mime_type = content_type
                break
        
        if not attachment_found:
            return jsonify({
                'success': False,
                'error': 'Attachment not found'
            }), 404
        
        # Create a unique filename to avoid collisions
        unique_id = str(uuid.uuid4())[:8]
        if '.' not in attachment_filename:
            # Try to guess the extension from mime type
            extension = mimetypes.guess_extension(attachment_mime_type) or ''
            filename_with_uuid = f"{attachment_filename}_{unique_id}{extension}"
        else:
            name, extension = os.path.splitext(attachment_filename)
            filename_with_uuid = f"{name}_{unique_id}{extension}"
        
        # Save the attachment to the uploads folder
        upload_folder = app.config['UPLOAD_FOLDER']
        email_attachments_folder = os.path.join(upload_folder, 'email_attachments')
        
        # Create the email attachments folder if it doesn't exist
        if not os.path.exists(email_attachments_folder):
            os.makedirs(email_attachments_folder)
        
        file_path = os.path.join(email_attachments_folder, filename_with_uuid)
        
        with open(file_path, 'wb') as f:
            f.write(attachment_content)
        
        # Create a download URL for the file
        download_url = url_for('download_file', filename=os.path.join('email_attachments', filename_with_uuid))
        
        return jsonify({
            'success': True,
            'filename': attachment_filename,
            'file_path': file_path,
            'download_url': download_url,
            'size': len(attachment_content),
            'mime_type': attachment_mime_type,
            'from': from_header,
            'subject': subject
        })
    except Exception as e:
        log.error(f"Error downloading attachment: {str(e)}")
        return jsonify({
            'success': False,
            'error': f'Error downloading attachment: {str(e)}'
        }), 500

@app.route('/api/email/process-attachments', methods=['POST'])
def process_email_attachments():
    """Process email attachments to extract invoice data"""
    try:
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
            
        attachment_paths = data.get('attachment_paths', [])
        from_emails = data.get('from', [])
        email_ids = data.get('email_ids', [])
        
        if not attachment_paths:
            return jsonify({
                'success': False,
                'error': 'No attachment paths provided'
            }), 400
        
        # Create batch ID for grouping these files
        batch_id = str(uuid.uuid4())
        log.info(f"Processing email batch {batch_id} with {len(attachment_paths)} attachments")
        
        # Process files one by one
        results = []
        
        for idx, file_path in enumerate(attachment_paths):
            if not os.path.exists(file_path):
                results.append({
                    'file_path': file_path,
                    'success': False,
                    'error': 'File not found'
                })
                continue
                
            if not allowed_file(file_path):
                results.append({
                    'file_path': file_path,
                    'success': False,
                    'error': 'Invalid file type. Only PDF files are supported.'
                })
                continue
            
            # Create source info from email data
            source_info = {
                'source': 'email',
                'from_email': from_emails[idx] if idx < len(from_emails) else None,
                'email_id': email_ids[idx] if idx < len(email_ids) else None
            }
            
            # Create a file storage-like object for the existing file
            class FilePathStorage:
                def __init__(self, path):
                    self.path = path
                    self.filename = os.path.basename(path)
                
                def save(self, target_path):
                    shutil.copy2(self.path, target_path)
            
            file_storage = FilePathStorage(file_path)
            
            # Process the file using the same processing logic as uploads
            # This ensures the same validation workflow
            result = process_invoice_file(
                file_storage,
                app.config, 
                lambda: get_db_connection(app.config['DATABASE']),
                lambda file_path: check_for_duplicate_invoice(
                    file_path, 
                    lambda x: check_invoice_exists(x, app.config['DATABASE']),
                    lambda file_path, **kwargs: InvoiceScanner(app.config['DATABASE'], app.config['ARCHIVE_DIR']).extract_invoice_data(file_path, store_in_db=False)
                ), 
                select_ai_model,
                lambda data, batch=None, source=None: save_to_pending(data, batch, source, db_path=app.config['DATABASE']),
                InvoiceScanner,
                batch_id=batch_id,
                source='email_import',
                source_info=source_info
            )
            
            # Add the result
            results.append(result)
        
        # Create a mapping of which email IDs had successful processing
        email_success_map = {}
        for idx, result in enumerate(results):
            if idx < len(email_ids):
                email_id = email_ids[idx]
                if email_id not in email_success_map:
                    email_success_map[email_id] = result.get('success', False)
        
        return jsonify({
            'success': True,
            'message': f'Processed {len(results)} attachments',
            'results': results,
            'batch_id': batch_id,
            'email_success_map': email_success_map
        })
    except Exception as e:
        log.error(f"Error processing email attachments: {str(e)}")
        return jsonify({
            'success': False,
            'error': f'Error processing email attachments: {str(e)}'
        }), 500

@app.route('/api/email/mark-processed', methods=['POST'])
def mark_emails_as_processed():
    """Mark emails as processed in the email server"""
    try:
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
            
        session_id = data.get('session_id')
        email_ids = data.get('email_ids', [])
        action = data.get('action', 'mark_read')  # mark_read, archive, delete
        
        if not session_id or not email_ids:
            return jsonify({
                'success': False,
                'error': 'Missing session_id or email_ids'
            }), 400
            
        if session_id not in email_connections:
            return jsonify({
                'success': False,
                'error': 'Email session not found or expired'
            }), 404
        
        mail = email_connections[session_id]['connection']
        
        # Make sure a mailbox is selected
        if not email_connections[session_id].get('selected_mailbox'):
            response, _ = mail.select('INBOX')
            if response != 'OK':
                return jsonify({
                    'success': False,
                    'error': 'Failed to select mailbox'
                }), 500
            email_connections[session_id]['selected_mailbox'] = 'INBOX'
        
        # Process each email ID
        processed_count = 0
        for email_id in email_ids:
            try:
                if action == 'mark_read':
                    # Mark as read (add Seen flag)
                    mail.store(email_id.encode(), '+FLAGS', '\\Seen')
                    processed_count += 1
                elif action == 'archive':
                    # First check if Archive folder exists
                    response, mailboxes = mail.list()
                    archive_folder = None
                    if response == 'OK':
                        for mailbox in mailboxes:
                            mb_decoded = mailbox.decode()
                            if 'Archive' in mb_decoded or 'Archiv' in mb_decoded:
                                parts = mb_decoded.split(' "." ')
                                if len(parts) > 1:
                                    archive_folder = parts[1].strip('"')
                                    break
                    
                    if archive_folder:
                        # Copy to Archive folder and mark for deletion
                        mail.copy(email_id.encode(), archive_folder)
                        mail.store(email_id.encode(), '+FLAGS', '\\Deleted')
                        processed_count += 1
                    else:
                        # Just mark as read if no Archive folder is found
                        mail.store(email_id.encode(), '+FLAGS', '\\Seen')
                        processed_count += 1
                elif action == 'delete':
                    # Mark for deletion
                    mail.store(email_id.encode(), '+FLAGS', '\\Deleted')
                    processed_count += 1
            except Exception as e:
                log.error(f"Error processing email {email_id}: {str(e)}")
        
        if action == 'delete' or action == 'archive':
            # Permanently remove emails marked for deletion
            mail.expunge()
        
        return jsonify({
            'success': True,
            'message': f'Successfully processed {processed_count} out of {len(email_ids)} emails',
            'processed_count': processed_count
        })
    except Exception as e:
        log.error(f"Error marking emails as processed: {str(e)}")
        return jsonify({
            'success': False,
            'error': f'Error marking emails as processed: {str(e)}'
        }), 500

@app.route('/download-file/<path:filename>')
def download_file(filename):
    """Download a file from the uploads directory"""
    try:
        # Prevent directory traversal attacks
        filename = os.path.normpath(filename).replace('\\', '/')
        base_dir = app.config['UPLOAD_FOLDER']
        
        # Build the full path
        file_path = os.path.join(base_dir, filename)
        
        # Verify the file exists and is within the uploads directory
        if not os.path.exists(file_path):
            return jsonify({
                'success': False,
                'error': 'File not found'
            }), 404
            
        # Make sure the file is within the uploads directory
        real_path = os.path.realpath(file_path)
        if not real_path.startswith(os.path.realpath(base_dir)):
            return jsonify({
                'success': False,
                'error': 'Access denied'
            }), 403
        
        # Determine the file's MIME type
        file_mimetype = mimetypes.guess_type(file_path)[0]
        if file_mimetype is None:
            file_mimetype = 'application/octet-stream'
        
        # Get just the filename without the path
        display_filename = os.path.basename(file_path)
        
        # Return the file as an attachment
        return send_file(
            file_path,
            mimetype=file_mimetype,
            as_attachment=True,
            download_name=display_filename
        )
    except Exception as e:
        log.error(f"Error downloading file {filename}: {str(e)}")
        return jsonify({
            'success': False,
            'error': f'Error downloading file: {str(e)}'
        }), 500

@app.route('/api/system-status')
def get_system_status():
    """Get system health information"""
    conn = None
    try:
        # Check database connection
        db_status = {
            'status': 'error',
            'message': 'Offline',
            'details': 'Could not connect to database'
        }
        
        try:
            conn = get_db_connection(app.config['DATABASE'])
            cursor = conn.execute('SELECT 1')
            if cursor.fetchone():
                db_status = {
                    'status': 'ok',
                    'message': 'Online',
                    'details': 'Database connection successful'
                }
        except Exception as db_err:
            db_status['details'] = str(db_err)
        
        # Check AI processing
        ai_status = {
            'status': 'unknown',
            'message': 'Unknown',
            'details': 'Could not verify AI system status'
        }
        
        try:
            # Try to check if Ollama is accessible
            OLLAMA_HOST = os.environ.get('OLLAMA_HOST', 'http://localhost:11434')
            response = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=2)
            
            if response.status_code == 200:
                models = [m["name"] for m in response.json().get("models", [])]
                ai_status = {
                    'status': 'ok',
                    'message': 'Available',
                    'details': f"Found {len(models)} models"
                }
            else:
                ai_status = {
                    'status': 'warning',
                    'message': 'Issues',
                    'details': f"Status code: {response.status_code}"
                }
        except Exception as ai_err:
            ai_status = {
                'status': 'error',
                'message': 'Offline',
                'details': str(ai_err)
            }
        
        # Check file storage
        storage_status = {
            'status': 'unknown',
            'message': 'Unknown',
            'details': 'Could not verify storage status'
        }
        
        try:
            # Check if storage directories exist and are writable
            storage_dirs = [
                app.config['UPLOAD_FOLDER'],
                app.config['ARCHIVE_DIR'],
                app.config['PREVIEW_FOLDER']
            ]
            
            for directory in storage_dirs:
                if not os.path.exists(directory):
                    raise Exception(f"Directory not found: {directory}")
                if not os.access(directory, os.W_OK):
                    raise Exception(f"Directory not writable: {directory}")
            
            # Get free space
            if sys.platform.startswith('win'):
                free_bytes = shutil.disk_usage(app.config['UPLOAD_FOLDER']).free
            else:
                stat = os.statvfs(app.config['UPLOAD_FOLDER'])
                free_bytes = stat.f_bavail * stat.f_frsize
            
            free_mb = free_bytes / (1024 * 1024)
            
            if free_mb < 100:  # Less than 100 MB
                storage_status = {
                    'status': 'warning',
                    'message': 'Low Space',
                    'details': f"Only {free_mb:.1f} MB available"
                }
            else:
                storage_status = {
                    'status': 'ok',
                    'message': 'Ready',
                    'details': f"{free_mb/1024:.1f} GB available"
                }
                
        except Exception as storage_err:
            storage_status = {
                'status': 'error',
                'message': 'Issues',
                'details': str(storage_err)
            }
            
        return jsonify({
            'success': True,
            'database': db_status,
            'ai_processing': ai_status,
            'file_storage': storage_status,
            'timestamp': datetime.now().isoformat()
        })
    
    except Exception as e:
        log.error(f"Error getting system status: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/invoice/<int:invoice_id>', methods=['PUT'])
def update_invoice(invoice_id):
    """Update invoice information"""
    conn = get_db_connection(app.config['DATABASE'])
    try:
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'No data provided'}), 400

        # Get the current invoice details to find supplier_id and company_id
        cursor = conn.execute(
            'SELECT * FROM invoices WHERE id = ?', 
            (invoice_id,)
        )
        invoice = cursor.fetchone()
        
        if not invoice:
            return jsonify({'success': False, 'error': 'Invoice not found'}), 404
        
        # Update supplier if needed
        supplier_id = invoice['supplier_id']
        if 'supplier_name' in data and data['supplier_name']:
            cursor = conn.execute('SELECT id FROM suppliers WHERE name = ?', (data['supplier_name'],))
            supplier = cursor.fetchone()
            if supplier:
                supplier_id = supplier['id']
            else:
                # Create new supplier
                cursor.execute('INSERT INTO suppliers (name) VALUES (?)', (data['supplier_name'],))
                supplier_id = cursor.lastrowid
        
        # Update company if needed
        company_id = invoice['company_id']
        if 'company_name' in data and data['company_name']:
            cursor = conn.execute('SELECT id FROM companies WHERE name = ?', (data['company_name'],))
            company = cursor.fetchone()
            if company:
                company_id = company['id']
            else:
                # Create new company
                cursor.execute('INSERT INTO companies (name) VALUES (?)', (data['company_name'],))
                company_id = cursor.lastrowid
        
        # Prepare update data
        update_fields = []
        params = []
        
        if 'invoice_number' in data:
            update_fields.append('invoice_number = ?')
            params.append(data['invoice_number'])
            
        if 'invoice_date' in data:
            update_fields.append('invoice_date = ?')
            params.append(data['invoice_date'])
            
        if 'amount' in data:
            update_fields.append('amount = ?')
            params.append(data['amount'])
            
        if 'description' in data:
            update_fields.append('description = ?')
            params.append(data['description'])
            
        # Always update the supplier_id and company_id
        update_fields.append('supplier_id = ?')
        params.append(supplier_id)
        update_fields.append('company_id = ?')
        params.append(company_id)
        
        # Add invoice_id to params
        params.append(invoice_id)
        
        # Execute update if there are fields to update
        if update_fields:
            query = f"UPDATE invoices SET {', '.join(update_fields)} WHERE id = ?"
            conn.execute(query, params)
            conn.commit()
            
            return jsonify({
                'success': True,
                'message': 'Invoice updated successfully',
                'invoice_id': invoice_id
            })
        else:
            return jsonify({'success': False, 'error': 'No fields to update'}), 400
            
    except Exception as e:
        conn.rollback()
        log.error(f"Error updating invoice: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/invoice/<int:invoice_id>', methods=['DELETE'])
def delete_invoice(invoice_id):
    """Delete an invoice and its associated file"""
    conn = get_db_connection(app.config['DATABASE'])
    try:
        # First get the file path from the invoice
        cursor = conn.execute('SELECT file_path FROM invoices WHERE id = ?', (invoice_id,))
        invoice = cursor.fetchone()
        
        if not invoice:
            return jsonify({'success': False, 'error': 'Invoice not found'}), 404
            
        file_path = invoice['file_path']
        
        # Delete from database
        conn.execute('DELETE FROM invoices WHERE id = ?', (invoice_id,))
        conn.commit()
        
        # Delete file from filesystem if it exists
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                log.info(f"Deleted file: {file_path}")
            except Exception as file_error:
                log.error(f"Error deleting file {file_path}: {str(file_error)}")
                # Don't return error, we've already deleted from database
        
        return jsonify({
            'success': True,
            'message': 'Invoice and associated file deleted successfully'
        })
    except Exception as e:
        conn.rollback()
        log.error(f"Error deleting invoice: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/batch-process-simple', methods=['POST'])
def batch_process_simple():
    """Process multiple invoice files at once"""
    if 'files[]' not in request.files:
        return jsonify({
            'success': False,
            'error': 'No files provided'
        }), 400
        
    files = request.files.getlist('files[]')
    
    if not files or len(files) == 0:
        return jsonify({
            'success': False, 
            'error': 'No files selected'
        }), 400
        
    if len(files) > 10:
        return jsonify({
            'success': False,
            'error': 'Maximum 10 files allowed'
        }), 400
    
    # Create batch ID for grouping these files
    batch_id = str(uuid.uuid4())
    log.info(f"Processing batch {batch_id} with {len(files)} files")
    
    # Process files one by one
    processed_files = []
    duplicate_count = 0
    
    try:
        for idx, file in enumerate(files):
            if not allowed_file(file.filename):
                processed_files.append({
                    'filename': file.filename,
                    'status': 'error',
                    'error': 'Invalid file type. Only PDF files are supported.',
                    'data': {}
                })
                continue
                
            # Process the file using the centralized invoice processor
            result = process_invoice_file(
                file, 
                app.config, 
                lambda: get_db_connection(app.config['DATABASE']),
                lambda file_path: check_for_duplicate_invoice(
                    file_path, 
                    lambda x: check_invoice_exists(x, app.config['DATABASE']),
                    lambda file_path, **kwargs: InvoiceScanner(app.config['DATABASE'], app.config['ARCHIVE_DIR']).extract_invoice_data(file_path, store_in_db=False)
                ), 
                select_ai_model,
                lambda data, batch=None, source=None: save_to_pending(data, batch, source, db_path=app.config['DATABASE']),
                InvoiceScanner,
                batch_id=batch_id,
                source='batch_upload'
            )
            
            if result['success']:
                # Format response for frontend consumption
                processed_files.append({
                    'filename': file.filename,
                    'status': 'processed',
                    'file_path': result['file_path'],
                    'preview_path': result.get('preview_path', ''),
                    'data': result['extracted_data'],
                    'pending_id': result['pending_id']
                })
            elif result.get('is_duplicate', False):
                # Handle duplicate
                duplicate_count += 1
                processed_files.append({
                    'filename': file.filename,
                    'status': 'duplicate',
                    'is_duplicate': True,
                    'duplicate_message': result.get('error', 'This invoice appears to be a duplicate'),
                    'file_path': result.get('file_path', ''),
                    'preview_path': result.get('preview_path', ''),
                    'data': {}
                })
            else:
                # Handle other errors
                processed_files.append({
                    'filename': file.filename,
                    'status': 'error',
                    'error': result.get('error', 'Error processing file'),
                    'file_path': result.get('file_path', ''),
                    'preview_path': result.get('preview_path', ''),
                    'data': {}
                })
        
        # Return all processed files info
        log.info(f"Batch {batch_id} processing completed. {len(processed_files)} files processed, {duplicate_count} duplicates")
        return jsonify({
            'success': True,
            'message': 'All files processed',
            'invoices': processed_files,
            'batch_id': batch_id,
            'duplicate_count': duplicate_count
        })
        
    except Exception as e:
        log.error(f"Error in batch processing: {str(e)}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/save-validated-invoices', methods=['POST'])
def save_validated_invoices():
    """Save multiple validated invoices"""
    conn = get_db_connection(app.config['DATABASE'])
    try:
        data = request.json
        if not data or 'invoices' not in data:
            return jsonify({
                'success': False,
                'error': 'No invoice data provided'
            }), 400
            
        invoices = data['invoices']
        if not invoices or len(invoices) == 0:
            return jsonify({
                'success': False,
                'error': 'No invoices to save'
            }), 400
        
        success_count = 0
        failed_count = 0
        
        for invoice in invoices:
            pending_id = invoice.get('pending_id')
            if not pending_id:
                failed_count += 1
                continue
                
            try:
                # First update the pending invoice with validated data
                update_data = {
                    'supplier_name': invoice.get('supplier_name', ''),
                    'company_name': invoice.get('company_name', ''),
                    'invoice_number': invoice.get('invoice_number', ''),
                    'invoice_date': invoice.get('invoice_date', ''),
                    'amount': invoice.get('amount', ''),
                    'vat_amount': invoice.get('vat_amount', ''),
                    'description': invoice.get('description', ''),
                    'is_validated': True,
                    'validated_at': datetime.now().isoformat(),
                    'validation_status': 'validated'
                }
                
                update_result = update_pending_invoice(pending_id, update_data, app.config['DATABASE'])
                
                if not update_result:
                    failed_count += 1
                    continue
                    
                # Then validate the invoice to move it to the main table
                try:
                    # Create a new request context for the validation route
                    with app.test_request_context(
                        f'/api/validate/{pending_id}',
                        method='POST',
                        data=json.dumps(update_data),
                        content_type='application/json'
                    ):
                        # Call the validation route function directly
                        validation_response = validate_invoice(pending_id)
                        
                        # Check if validation was successful
                        if isinstance(validation_response, tuple):
                            validation_data = validation_response[0]
                            if isinstance(validation_data, dict) and validation_data.get('success'):
                                success_count += 1
                            else:
                                failed_count += 1
                        elif hasattr(validation_response, 'json'):
                            validation_data = validation_response.json
                            if validation_data.get('success'):
                                success_count += 1
                            else:
                                failed_count += 1
                        else:
                            failed_count += 1
                except Exception as val_error:
                    log.error(f"Error validating invoice {pending_id}: {str(val_error)}")
                    failed_count += 1
            except Exception as e:
                log.error(f"Error saving validated invoice {pending_id}: {str(e)}")
                failed_count += 1
                
        return jsonify({
            'success': True,
            'message': f'Saved {success_count} invoices, {failed_count} failed',
            'success_count': success_count,
            'failed_count': failed_count
        })
                
    except Exception as e:
        log.error(f"Error saving validated invoices: {str(e)}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500
    finally:
        conn.close()

# Add a new validation-only endpoint for the two-stage upload process
@app.route('/api/validate-upload', methods=['POST'])
def validate_upload():
    """API endpoint for validating file upload before processing
    
    This is used in the two-stage upload process to check for duplicate invoices
    before fully processing the file.
    """
    if 'file' not in request.files:
        return jsonify({
            'success': False, 
            'error': 'No file provided'
        }), 400
    
    file = request.files['file']
    
    if file.filename == '':
        return jsonify({
            'success': False,
            'error': 'No file selected'
        }), 400
    
    try:
        # Ensure temp folder exists
        temp_dir = app.config['TEMP_FOLDER']
        os.makedirs(temp_dir, exist_ok=True)
        
        # Get and sanitize filename
        original_filename = file.filename
        filename = sanitize_filename(original_filename)
        
        # Check file extension
        if not allowed_file(filename):
            return jsonify({
                'success': False, 
                'error': 'File type not allowed. Only PDF files are supported.'
            }), 400
        
        # Create temporary file path and save file
        temp_file_path = os.path.join(temp_dir, filename)
        try:
            file.save(temp_file_path)
            log.info(f"Temporary file saved for validation: {temp_file_path}")
        except Exception as e:
            log.error(f"Error saving temporary file: {str(e)}")
            return jsonify({
                'success': False,
                'error': f"Error saving file: {str(e)}"
            }), 500
        
        # First, extract invoice data without checking duplicates or storing in DB
        extracted_data = None
        try:
            # Extract data from invoice to get the invoice number
            scanner = InvoiceScanner(app.config['DATABASE'], app.config['ARCHIVE_DIR'])
            extracted_data = scanner.extract_invoice_data(temp_file_path, store_in_db=False)
            
            # Make sure we have all the necessary fields in a consistent format
            if extracted_data:
                # Normalize the data fields to be consistent
                normalized_data = {
                    'supplier_name': extracted_data.get('supplier_name', '') or extracted_data.get('Lieferantename', ''),
                    'company_name': extracted_data.get('company_name', '') or extracted_data.get('Empfängerfirma', ''),
                    'invoice_number': extracted_data.get('invoice_number', '') or extracted_data.get('Rechnungsnummer', ''),
                    'invoice_date': extracted_data.get('invoice_date', '') or extracted_data.get('Rechnungsdatum', ''),
                    'amount': extracted_data.get('amount', '') or extracted_data.get('Gesamtbetrag', ''),
                    'vat_amount': extracted_data.get('vat_amount', '') or extracted_data.get('Mehrwertsteuerbetrag', ''),
                    'description': extracted_data.get('description', '') or extracted_data.get('Leistungsbeschreibung', '')
                }
                
                # Update extracted_data with normalized fields
                extracted_data.update(normalized_data)
            
            # Now that we have the invoice number, check if it's a duplicate
            invoice_number = None
            if extracted_data:
                if 'invoice_number' in extracted_data:
                    invoice_number = extracted_data['invoice_number']
                elif 'Rechnungsnummer' in extracted_data:
                    invoice_number = extracted_data['Rechnungsnummer']
                
            if invoice_number and invoice_number not in ['Unknown', 'Not found', 'Nicht gefunden', '']:
                # Check if this invoice number exists
                exists = check_invoice_exists(invoice_number, app.config['DATABASE'])
                if exists:
                    log.info(f"Duplicate invoice detected during validation: {invoice_number}")
                    # Clean up the temp file
                    try:
                        os.remove(temp_file_path)
                        log.info(f"Removed duplicate temp file: {temp_file_path}")
                    except Exception as e:
                        log.warning(f"Could not remove temp file: {str(e)}")
                        
                    return jsonify({
                        'success': False,
                        'is_duplicate': True,
                        'invoice_number': invoice_number,
                        'error': f"Invoice number {invoice_number} already exists in the database"
                    }), 200
        except Exception as e:
            log.error(f"Error extracting invoice data during validation: {str(e)}")
            # Continue even if extraction fails, we'll let the user see and fix the data
        
        # If we get here, the file is not a duplicate or we couldn't check
        # Return success with temp file path so user can validate the data
        return jsonify({
            'success': True,
            'message': 'File ready for validation',
            'temp_file_path': temp_file_path,
            'original_filename': original_filename,
            'extracted_data': extracted_data
        })
            
    except Exception as e:
        log.error(f"Error in validate_upload: {str(e)}", exc_info=True)
        return jsonify({
            'success': False,
            'error': f'Error validating file: {str(e)}'
        }), 500

@app.route('/api/batch-upload-sequential', methods=['POST'])
def batch_upload_sequential():
    """Upload multiple files sequentially for batch processing"""
    # Check if using pre-validated files (temp paths)
    temp_file_paths = request.form.getlist('temp_file_paths[]')
    original_filenames = request.form.getlist('original_filenames[]')
    
    if temp_file_paths and len(temp_file_paths) > 0:
        # Using pre-validated files
        files = []
        for i, temp_path in enumerate(temp_file_paths):
            if i < len(original_filenames):
                filename = original_filenames[i]
            else:
                filename = os.path.basename(temp_path)
                
            # Check if temp file exists
            if not os.path.exists(temp_path):
                return jsonify({
                    'success': False,
                    'error': f'Temporary file not found: {filename}'
                }), 404
                
            try:
                # Create a FileStorage-like object for each temp file
                with open(temp_path, 'rb') as f:
                    file_contents = f.read()
                
                class TempFileWrapper:
                    def __init__(self, name, content, path):
                        self.filename = name
                        self._content = content
                        self._file = io.BytesIO(content)
                        self.temp_path = path
                    
                    def save(self, path):
                        with open(path, 'wb') as f:
                            self._file.seek(0)
                            f.write(self._content)
                            
                    def read(self):
                        self._file.seek(0)
                        return self._file.read()
                
                files.append(TempFileWrapper(filename, file_contents, temp_path))
            except Exception as e:
                log.error(f"Error reading temp file {temp_path}: {str(e)}")
                return jsonify({
                    'success': False,
                    'error': f'Error reading temporary file {filename}: {str(e)}'
                }), 500
        
        # Files are already validated for duplicates
        skip_duplicate_check = True
    else:
        # Regular file upload approach
        if 'files[]' not in request.files:
            return jsonify({
                'success': False,
                'error': 'No files provided'
            }), 400
            
        files = request.files.getlist('files[]')
        
        if not files or len(files) == 0:
            return jsonify({
                'success': False,
                'error': 'No files selected'
            }), 400
        
        # We need to check for duplicates
        skip_duplicate_check = False
    
    # Create a batch ID
    batch_id = str(uuid.uuid4())
    log.info(f"Created batch {batch_id} with {len(files)} files")
    
    # Initialize batch in database
    conn = get_db_connection(app.config['DATABASE'])
    
    try:
        # Create batch entry
        cursor = conn.execute('''
            INSERT INTO batches (
                id, name, file_count, status, created_at
            ) VALUES (?, ?, ?, ?, ?)
        ''', (
            batch_id,
            f"Batch {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            len(files),
            'processing',
            datetime.now().isoformat()
        ))
        conn.commit()
        
        # Process files in the batch for AI extraction only
        batch_files = []
        for file in files:
            try:
                # Temporary path to clean up when we're done
                temp_path = None
                
                # If this is a pre-validated file wrapper, get its original path
                if hasattr(file, '_file') and hasattr(file, '_content'):
                    original_filename = file.filename
                    
                    # If we already have a temp path, use it
                    if hasattr(file, 'temp_path') and os.path.exists(file.temp_path):
                        temp_path = file.temp_path
                        is_in_temp = True
                    else:
                        # Create a temp copy we can safely process
                        temp_dir = app.config['TEMP_FOLDER']
                        os.makedirs(temp_dir, exist_ok=True)
                        temp_path = os.path.join(temp_dir, sanitize_filename(original_filename))
                        file.save(temp_path)
                        is_in_temp = True
                    
                    file_path = temp_path
                    
                else:
                    # Regular file upload
                    original_filename = file.filename
                    is_in_temp = False
                
                # Use standard upload logic for individual files, but skip dupe check if pre-validated
                # Also set store_in_db=False to prevent database insertion before human validation
                result = process_invoice_file(
                    file, 
                    app.config,
                    lambda: conn,
                    lambda file_path: {} if skip_duplicate_check else check_for_duplicate_invoice(
                        file_path, 
                        lambda x: check_invoice_exists(x, app.config['DATABASE']),
                        lambda file_path, **kwargs: InvoiceScanner(
                            app.config['DATABASE'], 
                            app.config['ARCHIVE_DIR']
                        ).extract_invoice_data(file_path, store_in_db=False)
                    ),
                    select_ai_model,
                    lambda data, batch=None, source=None: save_to_pending(
                        data, batch, source, db_path=app.config['DATABASE']
                    ),
                    InvoiceScanner,
                    batch_id=batch_id,
                    source='batch_upload_sequential'
                )
                
                # Don't clean up temp files since we need them for human validation
                # We'll clean them up after final validation
                
                # Add file info to batch list
                if result['success']:
                    batch_files.append({
                        'file_path': result['file_path'],
                        'preview_path': result.get('preview_path', ''),
                        'filename': original_filename,
                        'pending_id': result['pending_id'],
                        'extracted_data': result.get('extracted_data', {}),
                        'success': True
                    })
                    
                    # Add to batch queue
                    cursor = conn.execute('''
                        INSERT INTO batch_queue (
                            batch_id, file_path, preview_path, filename, status, pending_id, position
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        batch_id,
                        result['file_path'],
                        result.get('preview_path', ''),
                        original_filename,
                        'pending_validation',  # Changed from 'pending' to 'pending_validation'
                        result['pending_id'],
                        len(batch_files)
                    ))
                    conn.commit()
                else:
                    # Add failed file info
                    batch_files.append({
                        'filename': original_filename,
                        'error': result.get('error', 'Unknown error'),
                        'success': False,
                        'is_duplicate': result.get('is_duplicate', False)
                    })
                    log.warning(f"Failed to process file {original_filename} in batch: {result.get('error')}")
            except Exception as e:
                log.error(f"Error processing batch file {file.filename}: {str(e)}")
                batch_files.append({
                    'filename': file.filename,
                    'error': str(e),
                    'success': False
                })
        
        # Update batch status to ready for validation
        conn.execute('''
            UPDATE batches SET status = ?, updated_at = ? WHERE id = ?
        ''', ('ready_for_validation', datetime.now().isoformat(), batch_id))
        conn.commit()
        
        # Determine if we should redirect to sequential validation or return files for in-page validation
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            # For AJAX requests, return the files data for in-page validation
            return jsonify({
                'success': True,
                'message': f'Processed {len(batch_files)} files',
                'batch_id': batch_id,
                'files': batch_files
            })
        else:
            # For regular form submissions, redirect to sequential validation page
            return jsonify({
                'success': True,
                'message': f'Processed {len(batch_files)} files',
                'batch_id': batch_id,
                'redirect': f'/sequential-validate/{batch_id}',
                'files': batch_files
            })
        
    except Exception as e:
        log.error(f"Error in batch processing: {str(e)}", exc_info=True)
        conn.rollback()
        
        
        # Don't clean up temp files on error - they might be useful for debugging
                    
        return jsonify({
            'success': False,
            'error': f'Error processing batch: {str(e)}'
        }), 500
        
    finally:
        conn.close()

@app.route('/api/finalize-batch', methods=['POST'])
def finalize_batch():
    """Finalize a batch of invoices after human validation"""
    try:
        data = request.json
        if not data or 'batch_id' not in data:
            return jsonify({
                'success': False,
                'error': 'Missing batch_id or validation data'
            }), 400
        
        batch_id = data.get('batch_id')
        validated_files = data.get('validated_files', [])
        
        if not validated_files:
            return jsonify({
                'success': False,
                'error': 'No validated files provided'
            }), 400
        
        conn = get_db_connection(app.config['DATABASE'])
        
        # Check if the batch exists
        cursor = conn.execute('SELECT id, status FROM batches WHERE id = ?', (batch_id,))
        batch = cursor.fetchone()
        
        if not batch:
            conn.close()
            return jsonify({
                'success': False,
                'error': f'Batch {batch_id} not found'
            }), 404
        
        # Process each validated file
        success_count = 0
        error_count = 0
        error_details = []
        temp_files_to_clean = []
        
        for file_data in validated_files:
            try:
                pending_id = file_data.get('pending_id')
                if not pending_id:
                    error_count += 1
                    error_details.append({
                        'file': file_data.get('file_path', 'Unknown file'),
                        'error': 'Missing pending_id'
                    })
                    continue
                
                # Get the pending invoice data from database
                cursor = conn.execute('SELECT * FROM pending_invoices WHERE id = ?', (pending_id,))
                pending = cursor.fetchone()
                
                if not pending:
                    error_count += 1
                    error_details.append({
                        'file': file_data.get('file_path', 'Unknown file'),
                        'error': f'Pending invoice with ID {pending_id} not found'
                    })
                    continue
                
                # First, check for duplicates with the validated invoice number
                invoice_number = file_data.get('invoice_number')
                if invoice_number:
                    cursor = conn.execute(
                        'SELECT id FROM invoices WHERE invoice_number = ?', 
                        (invoice_number,)
                    )
                    existing = cursor.fetchone()
                    
                    if existing:
                        error_count += 1
                        error_details.append({
                            'file': pending['file_path'],
                            'error': f'Invoice number {invoice_number} already exists in database',
                            'is_duplicate': True,
                            'invoice_number': invoice_number
                        })
                        continue
                
                # Update the pending invoice with validated data
                update_fields = []
                params = []
                
                for field in ['supplier_name', 'company_name', 'invoice_number', 
                              'invoice_date', 'amount', 'vat_amount', 'description']:
                    if field in file_data:
                        update_fields.append(f"{field} = ?")
                        params.append(file_data[field])
                
                update_fields.append("is_validated = ?")
                params.append(1)
                update_fields.append("validated_at = ?")
                params.append(datetime.now().isoformat())
                update_fields.append("validation_status = ?")
                params.append('human_validated')
                
                params.append(pending_id)
                
                query = f"UPDATE pending_invoices SET {', '.join(update_fields)} WHERE id = ?"
                conn.execute(query, params)
                
                # Now process the validated invoice into the main table
                
                # Get or create supplier
                supplier_id = None
                supplier_name = file_data.get('supplier_name') or pending['supplier_name']
                
                if supplier_name:
                    cursor = conn.execute('SELECT id FROM suppliers WHERE name = ?', (supplier_name,))
                    supplier = cursor.fetchone()
                    
                    if supplier:
                        supplier_id = supplier['id']
                    else:
                        cursor.execute('INSERT INTO suppliers (name) VALUES (?)', (supplier_name,))
                        supplier_id = cursor.lastrowid
                
                # Get or create company
                company_id = None
                company_name = file_data.get('company_name') or pending['company_name']
                
                if company_name:
                    cursor = conn.execute('SELECT id FROM companies WHERE name = ?', (company_name,))
                    company = cursor.fetchone()
                    
                    if company:
                        company_id = company['id']
                    else:
                        cursor.execute('INSERT INTO companies (name) VALUES (?)', (company_name,))
                        company_id = cursor.lastrowid
                
                # Parse amounts
                amount_value = 0
                amount_str = file_data.get('amount') or pending['amount']
                if amount_str:
                    try:
                        amount_value = normalize_amount(amount_str)
                    except:
                        log.warning(f"Could not parse amount: {amount_str}")
                
                vat_value = 0
                vat_str = file_data.get('vat_amount') or pending['vat_amount']
                if vat_str:
                    try:
                        vat_value = normalize_amount(vat_str)
                    except:
                        log.warning(f"Could not parse VAT amount: {vat_str}")
                
                # Get the original file path
                file_path = pending['file_path']
                original_path = pending['original_path']
                
                # Add temp file path to cleanup list if in temp folder
                if file_path and app.config['TEMP_FOLDER'] in file_path:
                    temp_files_to_clean.append(file_path)
                
                # Insert into the main invoices table
                cursor.execute('''
                    INSERT INTO invoices (
                        file_path, original_path, invoice_number, invoice_date, 
                        amount, amount_original, vat_amount, vat_amount_original,
                        description, supplier_id, company_id, processed_at,
                        source_info
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    file_path,
                    original_path,
                    file_data.get('invoice_number') or pending['invoice_number'],
                    file_data.get('invoice_date') or pending['invoice_date'],
                    amount_value,
                    amount_str,
                    vat_value,
                    vat_str,
                    file_data.get('description') or pending['description'],
                    supplier_id,
                    company_id,
                    datetime.now().isoformat(),
                    f"From batch {batch_id}, validated by human"
                ))
                
                # Mark the pending invoice as finalized
                conn.execute('''
                    UPDATE pending_invoices 
                    SET is_finalized = 1, finalized_at = ? 
                    WHERE id = ?
                ''', (datetime.now().isoformat(), pending_id))
                
                # Update the batch queue item status
                conn.execute('''
                    UPDATE batch_queue 
                    SET status = 'processed' 
                    WHERE batch_id = ? AND pending_id = ?
                ''', (batch_id, pending_id))
                
                success_count += 1
                log.info(f"Invoice {file_data.get('invoice_number') or pending['invoice_number']} validated and saved successfully")
                
            except Exception as e:
                log.error(f"Error finalizing invoice: {str(e)}", exc_info=True)
                error_count += 1
                error_details.append({
                    'file': file_data.get('file_path', 'Unknown file'),
                    'error': str(e)
                })
        
        # Update batch status based on results
        if success_count > 0:
            if error_count == 0:
                batch_status = 'completed'
            else:
                batch_status = 'partially_completed'
        else:
            batch_status = 'failed'
        
        conn.execute('''
            UPDATE batches 
            SET status = ?, success_count = ?, error_count = ?, updated_at = ? 
            WHERE id = ?
        ''', (batch_status, success_count, error_count, datetime.now().isoformat(), batch_id))
        
        # Commit all changes
        conn.commit()
        
        # Clean up temporary files
        for temp_file in temp_files_to_clean:
            try:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
                    log.info(f"Cleaned up temporary file: {temp_file}")
            except Exception as e:
                log.warning(f"Error cleaning up temporary file {temp_file}: {str(e)}")
        
        return jsonify({
            'success': True,
            'message': f'Batch processing complete: {success_count} files processed, {error_count} errors',
            'success_count': success_count,
            'error_count': error_count,
            'errors': error_details,
            'batch_id': batch_id,
            'status': batch_status
        })
        
    except Exception as e:
        log.error(f"Error finalizing batch: {str(e)}", exc_info=True)
        return jsonify({
            'success': False,
            'error': f'Error finalizing batch: {str(e)}'
        }), 500
    finally:
        if 'conn' in locals():
            conn.close()

@app.route('/api/email/keepalive', methods=['POST'])
def email_keepalive():
    """Keep an email session alive by sending a NOOP command"""
    try:
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
            
        session_id = data.get('session_id')
        if not session_id:
            return jsonify({'success': False, 'error': 'No session ID provided'}), 400
            
        if session_id not in email_connections:
            return jsonify({
                'success': False, 
                'error': 'Email session not found or expired',
                'reconnect_required': True
            }), 404
            
        mail = email_connections[session_id]['connection']
        
        # Send NOOP command to keep connection alive
        try:
            response, data = mail.noop()
            
            if response == 'OK':
                # Update the timestamp in the session data
                email_connections[session_id]['last_activity'] = datetime.now()
                
                return jsonify({
                    'success': True,
                    'message': 'Session refreshed successfully',
                    'session_id': session_id
                })
            else:
                return jsonify({
                    'success': False,
                    'error': f'Failed to refresh session: {response}',
                    'reconnect_required': True
                }), 500
        except Exception as noop_error:
            log.error(f"Error sending NOOP command: {str(noop_error)}")
            return jsonify({
                'success': False,
                'error': f'IMAP error: {str(noop_error)}',
                'reconnect_required': True
            }), 500
    except Exception as e:
        log.error(f"Error in keepalive: {str(e)}")
        return jsonify({
            'success': False,
            'error': f'Error refreshing session: {str(e)}',
            'reconnect_required': True
        }), 500

if __name__ == '__main__':
    app.run(debug=True)
