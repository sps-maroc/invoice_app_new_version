import os
import shutil
import logging
import json
from datetime import datetime
from werkzeug.utils import secure_filename
import sys
import traceback

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from parser import validate_invoice_data, normalize_amount

# Setup logging
log = logging.getLogger(__name__)

def sanitize_filename_util(filename):
    """Sanitize filename to prevent path traversal and other security issues"""
    return secure_filename(filename)

def allowed_file(filename, allowed_extensions=None):
    """Check if the file extension is allowed"""
    if allowed_extensions is None:
        allowed_extensions = {'pdf'}
    
    return '.' in filename and filename.lower().split('.')[-1] in allowed_extensions

def create_preview(file_path, preview_dir):
    """Create a preview copy of the file"""
    try:
        filename = os.path.basename(file_path)
        preview_path = os.path.join(preview_dir, f"preview_{filename}")
        
        # Create preview directory if it doesn't exist
        os.makedirs(os.path.dirname(preview_path), exist_ok=True)
        
        # Copy the file to the preview location
        shutil.copy2(file_path, preview_path)
        
        return preview_path
    except Exception as e:
        log.error(f"Error creating preview: {str(e)}", exc_info=True)
        return None

def process_invoice_file(file_storage, app_config, db_conn_func, check_invoice_exists_func, 
                         select_ai_model_func, save_to_pending_func, InvoiceScannerClass,
                         batch_id=None, source='upload', current_user=None):
    """Process an uploaded invoice file with consistent AI model usage
    
    This function handles both batch and single uploads consistently:
    1. Saves the uploaded file
    2. Checks for duplicates
    3. Creates preview
    4. Selects appropriate AI model
    5. Extracts data using the model
    6. Saves to pending table
    
    Args:
        file_storage: The uploaded file (FileStorage or FileStorage-like object)
        app_config: Flask application config
        db_conn_func: Function to get database connection
        check_invoice_exists_func: Function to check if invoice exists
        select_ai_model_func: Function to select AI model
        save_to_pending_func: Function to save to pending table
        InvoiceScannerClass: The InvoiceScanner class
        batch_id: Batch ID if part of batch upload
        source: Source of upload (single_upload, batch_upload, etc.)
        current_user: Current user info if available
        
    Returns:
        dict: Result of processing with success status
    """
    try:
        # Ensure upload folder exists
        os.makedirs(app_config['UPLOAD_FOLDER'], exist_ok=True)
        os.makedirs(app_config['PREVIEW_FOLDER'], exist_ok=True)
        
        # Get and sanitize filename
        original_filename = file_storage.filename
        filename = sanitize_filename_util(original_filename)
        
        # Check file extension
        if not allowed_file(filename, app_config.get('ALLOWED_EXTENSIONS')):
            return {
                'success': False, 
                'error': 'File type not allowed. Only PDF files are supported.',
                'filename': original_filename
            }
            
        # Create file path and save file
        file_path = os.path.join(app_config['UPLOAD_FOLDER'], filename)
        try:
            file_storage.save(file_path)
            log.info(f"File saved successfully to {file_path}")
        except Exception as e:
            log.error(f"Error saving file {filename}: {str(e)}", exc_info=True)
            return {
                'success': False,
                'error': f"Error saving file: {str(e)}",
                'filename': original_filename
            }
            
        # Check for duplicate invoices before further processing
        try:
            duplicate_check = check_invoice_exists_func(file_path)
            if duplicate_check.get('is_duplicate', False):
                log.warning(f"Duplicate invoice detected: {filename} - {duplicate_check.get('invoice_number', 'unknown')}")
                return {
                    'success': False,
                    'is_duplicate': True,
                    'error': duplicate_check.get('error', 'Duplicate invoice detected'),
                    'invoice_number': duplicate_check.get('invoice_number'),
                    'filename': original_filename,
                    'file_path': file_path
                }
        except Exception as duplicate_error:
            log.error(f"Error checking for duplicates: {str(duplicate_error)}", exc_info=True)
            # We'll continue processing even if duplicate check fails
            
        # Create preview file
        preview_path = create_preview(file_path, app_config['PREVIEW_FOLDER'])
        if not preview_path:
            log.warning(f"Could not create preview for {filename}, using original file path")
            preview_path = file_path  # Fallback to original file path
            
        log.info(f"Processing {filename} with AI extraction")
        
        # Select appropriate AI model based on file size
        try:
            file_size = os.path.getsize(file_path)
            selected_model = select_ai_model_func(file_path, file_size)
            log.info(f"Selected AI model for {filename}: {selected_model}")
        except Exception as model_error:
            log.error(f"Error selecting AI model: {str(model_error)}", exc_info=True)
            selected_model = "llama3:latest"  # Default model as fallback
            log.info(f"Using fallback model: {selected_model}")
        
        # Initialize InvoiceScanner
        scanner = None
        extracted_data = {}
        extraction_successful = False
        raw_text = ""
        ocr_text = ""
        
        try:
            scanner = InvoiceScannerClass(
                app_config['DATABASE'],
                app_config['ARCHIVE_DIR']
            )
            
            # Extract text from file
            raw_text, ocr_text = scanner.extract_text_from_pdf(file_path)
            
            if raw_text == "SKIP_PROCESSING":
                log.warning(f"File {filename} doesn't appear to be an invoice - skipping AI processing")
                extracted_data = {
                    'invoice_number': os.path.splitext(filename)[0],
                    'invoice_date': datetime.now().strftime('%Y-%m-%d'),
                    'amount': '',
                    'vat_amount': '',
                    'supplier_name': 'Unknown Supplier',
                    'company_name': '',
                    'description': f'This file does not appear to be an invoice',
                    'success': False,
                    'error': 'File does not appear to be an invoice',
                    'needs_manual_input': True
                }
            else:
                # Process with AI model
                model_result = scanner.process_with_model(raw_text, selected_model)
                
                if isinstance(model_result, dict):
                    # Validate and normalize the data
                    validated_data = validate_invoice_data(model_result)
                    
                    if validated_data.get('validation_success', False):
                        log.info(f"Successfully validated invoice data for {filename}")
                        extracted_data = validated_data
                        extraction_successful = True
                    else:
                        log.warning(f"Validation failed for {filename}, using raw model output")
                        extracted_data = model_result
                        extraction_successful = model_result.get('success', False)
                else:
                    log.error(f"Unexpected model result type: {type(model_result)}")
                    extracted_data = {
                        'success': False,
                        'error': 'Unexpected model result format',
                        'needs_manual_input': True
                    }
        except Exception as ai_error:
            log.error(f"AI processing error for {filename}: {str(ai_error)}", exc_info=True)
            extracted_data = {
                'invoice_number': os.path.splitext(filename)[0],
                'invoice_date': datetime.now().strftime('%Y-%m-%d'),
                'amount': '',
                'vat_amount': '',
                'supplier_name': 'Unknown Supplier',
                'company_name': '',
                'description': f'Error during AI processing: {str(ai_error)}',
                'success': False,
                'error': str(ai_error),
                'needs_manual_input': True
            }
        finally:
            if scanner:
                try:
                    scanner.close()
                except:
                    pass
            
        # Create normalized form data for frontend display and database storage
        form_data = {
            'file_path': file_path,
            'preview_path': preview_path,
            'supplier_name': extracted_data.get('supplier_name', '') or extracted_data.get('Lieferantename', ''),
            'company_name': extracted_data.get('company_name', '') or extracted_data.get('Empfängerfirma', ''),
            'invoice_number': extracted_data.get('invoice_number', '') or extracted_data.get('Rechnungsnummer', ''),
            'invoice_date': extracted_data.get('invoice_date', '') or extracted_data.get('Rechnungsdatum', ''),
            'amount_original': extracted_data.get('amount', '') or extracted_data.get('Gesamtbetrag', ''),
            'vat_amount_original': extracted_data.get('vat_amount', '') or extracted_data.get('Mehrwertsteuerbetrag', ''),
            'description': extracted_data.get('description', '') or extracted_data.get('Leistungsbeschreibung', ''),
            'Lieferantename': extracted_data.get('Lieferantename', '') or extracted_data.get('supplier_name', ''),
            'Empfängerfirma': extracted_data.get('Empfängerfirma', '') or extracted_data.get('company_name', ''),
            'Rechnungsnummer': extracted_data.get('Rechnungsnummer', '') or extracted_data.get('invoice_number', ''),
            'Rechnungsdatum': extracted_data.get('Rechnungsdatum', '') or extracted_data.get('invoice_date', ''),
            'Gesamtbetrag': extracted_data.get('Gesamtbetrag', '') or extracted_data.get('amount', ''),
            'Mehrwertsteuerbetrag': extracted_data.get('Mehrwertsteuerbetrag', '') or extracted_data.get('vat_amount', ''),
            'Leistungsbeschreibung': extracted_data.get('Leistungsbeschreibung', '') or extracted_data.get('description', '')
        }
            
        # Additional fields from enhanced extraction
        if extracted_data.get('due_date'):
            form_data['due_date'] = extracted_data['due_date']
            
        if extracted_data.get('currency'):
            form_data['currency'] = extracted_data['currency']
            
        # Source info for database
        source_info = {
            'source': source,
            'batch_id': batch_id,
            'processed_at': datetime.now().isoformat(),
            'filename': original_filename,
            'model_used': selected_model,
            'success': extraction_successful
        }
        
        # Add user info if available
        if current_user:
            source_info['user'] = current_user
            
        # Create database record
        pending_invoice_data = {
            **form_data,  # Include all normalized form data
            'original_path': file_path,
            'needs_manual_input': not extraction_successful,
            'extracted_data': json.dumps(extracted_data),
            'raw_text': raw_text[:10000] if isinstance(raw_text, str) else '',  # Limit text size
            'ocr_text': ocr_text[:10000] if isinstance(ocr_text, str) else '',  # Limit OCR text size
            'source': source,
            'source_info': json.dumps(source_info),
            'batch_id': batch_id,
            'validation_status': 'pending_validation'
        }
        
        # Try to save to database
        try:
            pending_id = save_to_pending_func(pending_invoice_data, batch_id, source_info)
            form_data['pending_id'] = pending_id
            
            log.info(f"Invoice data saved to pending_invoices with ID {pending_id}")
            
            # Return data for the frontend
            return {
                'success': True,
                'filename': filename,
                'file_path': file_path,
                'preview_path': preview_path,
                'pending_id': pending_id,
                'extracted_data': form_data,
                'needs_validation': True,
                'status': 'processed'
            }
            
        except Exception as db_error:
            log.error(f"Database error saving invoice {filename}: {str(db_error)}", exc_info=True)
            return {
                'success': False,
                'filename': original_filename,
                'error': f"Database error: {str(db_error)}",
                'file_path': file_path,
                'preview_path': preview_path,
                'extracted_data': form_data,
                'status': 'error'
            }
            
    except Exception as e:
        log.error(f"Unhandled error processing file {file_storage.filename if hasattr(file_storage, 'filename') else 'unknown file'}: {str(e)}", exc_info=True)
        return {
            'success': False,
            'filename': getattr(file_storage, 'filename', "unknown_file"),
            'error': f"Processing error: {str(e)}",
            'status': 'error'
        } 