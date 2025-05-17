import os
import json
import shutil
import uuid
import logging
from datetime import datetime
from werkzeug.utils import secure_filename
import re

# Local imports - refactored to avoid circular imports
from utils.file_utils import sanitize_filename

# Assuming InvoiceScanner is in the same directory or accessible via PYTHONPATH
# If app.py is in the parent directory, adjust import path if needed.
# from ..app import select_ai_model, check_invoice_exists, save_to_pending, sanitize_filename  # Example if app.py is one level up
# For now, these will be expected to be passed or handled via app_context or direct import if in same level.

log = logging.getLogger(__name__)

# Moved from app.py - ensure this is the same as in app.py
def sanitize_filename_util(filename):
    """
    Custom function to sanitize filename while preserving spaces and parentheses.
    This avoids the double underscore problem with secure_filename.
    """
    filename = re.sub(r'[^\w\s\(\)\.\-€]', '_', filename)
    filename = re.sub(r'\s+', ' ', filename)
    filename = filename.strip()
    if not filename:
        filename = "invoice.pdf"
    return filename

def process_invoice_file(file_storage, app_config, db_conn_func, 
                         check_invoice_exists_func, select_ai_model_func, 
                         save_to_pending_func, InvoiceScannerClass, 
                         batch_id=None, source='single_upload', source_info=None):
    """
    Processes a single PDF invoice file.
    Returns a dictionary with processing results.

    Args:
        file_storage: The uploaded file object
        app_config: Flask app configuration
        db_conn_func: Function to get database connection
        check_invoice_exists_func: Function to check if invoice exists
        select_ai_model_func: Function to select AI model
        save_to_pending_func: Function to save pending invoice 
        InvoiceScannerClass: InvoiceScanner class reference
        batch_id: Optional batch ID for grouped processing
        source: Source of upload (e.g. 'single_upload', 'batch_upload')
        source_info: Additional source information

    Returns:
        dict: Processing result with file information and extracted data
    """
    filename_orig = file_storage.filename
    filename = sanitize_filename(filename_orig)

    file_path = os.path.join(app_config['UPLOAD_FOLDER'], filename)
    preview_path = os.path.join(app_config['PREVIEW_FOLDER'], f"preview_{filename}")

    try:
        # Create directories if needed
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        os.makedirs(os.path.dirname(preview_path), exist_ok=True)

        file_storage.save(file_path)
        log.info(f"Saved file to {file_path}")
        shutil.copy2(file_path, preview_path)
        log.info(f"Created preview file at {preview_path}")

        # 1. Duplicate Check
        duplicate_check = check_invoice_exists_func(file_path)
        if duplicate_check.get('is_duplicate', False):
            log.warning(f"Duplicate detected: {filename} - {duplicate_check.get('message')}")
            return {
                'success': False, 'is_duplicate': True, 'filename': filename,
                'file_path': file_path, 'preview_path': preview_path,
                'error': duplicate_check.get('message', 'Duplicate invoice'), 'status': 'duplicate'
            }

        # 2. AI Model Selection & Processing
        file_size = os.path.getsize(file_path)
        model_name = select_ai_model_func(file_path, file_size)
        log.info(f"Selected AI model {model_name} for processing {filename} ({file_size / 1024:.1f} KB)")

        scanner = InvoiceScannerClass(app_config['DATABASE'], app_config['ARCHIVE_DIR'])
        extracted_data = {}
        try:
            text, ocr_text = scanner.extract_text_from_pdf(file_path)
            log.info(f"Extracted text from {filename}")
            
            # Only process if not marked as SKIP_PROCESSING
            if text != "SKIP_PROCESSING":
                model_result = scanner.process_with_model(text, model_name)
                log.info(f"Processed text with model {model_name}")
                extracted_data = model_result
            else:
                log.info(f"Skipped processing for {filename} - does not appear to be an invoice")
                extracted_data = {
                    'success': False,
                    'error': 'File does not appear to be an invoice',
                    'needs_manual_input': True
                }
                
            if not (extracted_data and extracted_data.get('success', False)):
                log.warning(f"Failed to extract data from {filename} - needs manual review")
        except Exception as e_process:
            log.error(f"Error during AI processing for {filename}: {str(e_process)}")
            extracted_data = {
                'Lieferantename': '', 'Rechnungsdatum': '', 'Gesamtbetrag': '',
                'Empfängerfirma': '', 'Rechnungsnummer': '', 'Mehrwertsteuerbetrag': '',
                'Leistungsbeschreibung': '', 'success': False, 'error': str(e_process)
            }
        finally:
            scanner.close()

        # 3. Data Normalization
        form_data = {
            'file_path': file_path, 'preview_path': preview_path,
            'supplier_name': '', 'company_name': '', 'invoice_number': '',
            'invoice_date': '', 'amount': '', 'vat_amount': '', 'description': '',
            'Lieferantename': '', 'Empfängerfirma': '', 'Rechnungsnummer': '',
            'Rechnungsdatum': '', 'Gesamtbetrag': '', 'Mehrwertsteuerbetrag': '',
            'Leistungsbeschreibung': ''
        }
        
        if isinstance(extracted_data, dict):
            log.info(f"Raw extracted data (processing.py): {json.dumps(extracted_data, indent=2)}")

            data_sources = [
                extracted_data, 
                extracted_data.get('invoice_data', {}), 
                extracted_data.get('data', {})
            ]
            field_maps = {
                'supplier_name': ['supplier_name', 'Lieferantename', 'vendor_name', 'supplier', 'vendor'],
                'company_name': ['company_name', 'Empfängerfirma', 'recipient', 'company'],
                'invoice_number': ['invoice_number', 'Rechnungsnummer', 'invoice_id', 'invoice #', 'invoice_no'],
                'invoice_date': ['invoice_date', 'Rechnungsdatum', 'date', 'invoice_dt'],
                'amount': ['amount', 'Gesamtbetrag', 'total_amount', 'total', 'invoice_amount', 'gross_amount'],
                'vat_amount': ['vat_amount', 'Mehrwertsteuerbetrag', 'tax_amount', 'vat', 'tax', 'sales_tax'],
                'description': ['description', 'Leistungsbeschreibung', 'details', 'service_description', 'invoice_description']
            }

            # Try to find values from all data sources
            for target_field, source_fields in field_maps.items():
                for data_source in data_sources:
                    if not isinstance(data_source, dict): continue
                    
                    for source_field in source_fields:
                        if source_field in data_source and data_source[source_field]:
                            form_data[target_field] = data_source[source_field]
                            # Map German/English field pairs
                            german_key_map = {
                                'supplier_name': 'Lieferantename', 'company_name': 'Empfängerfirma',
                                'invoice_number': 'Rechnungsnummer', 'invoice_date': 'Rechnungsdatum',
                                'amount': 'Gesamtbetrag', 'vat_amount': 'Mehrwertsteuerbetrag',
                                'description': 'Leistungsbeschreibung'
                            }
                            english_key_map = {v: k for k, v in german_key_map.items()}
                            
                            if target_field in german_key_map:
                                form_data[german_key_map[target_field]] = data_source[source_field]
                            elif target_field in english_key_map:
                                form_data[english_key_map[target_field]] = data_source[source_field]
                            break
                    
                    if form_data[target_field]:
                        break
            
            # Create a flat structure for nested objects
            flat_data = {}
            def flatten_dict(d, parent_key=''):
                if not isinstance(d, dict):
                    return
                    
                for k, v in d.items():
                    if isinstance(v, dict):
                        flatten_dict(v, f"{parent_key}{k}_")
                    else:
                        flat_data[f"{parent_key}{k}"] = v
                        
            # Flatten all data sources
            for data_source in data_sources:
                if isinstance(data_source, dict):
                    flatten_dict(data_source)

            # Try to find matches in flattened data
            for target_field, source_fields in field_maps.items():
                if not form_data[target_field]:  # Only look if we haven't found it yet
                    for key, val_flat in flat_data.items():
                        for source_field in source_fields:
                            if source_field.lower() in key.lower() and val_flat:
                                form_data[target_field] = val_flat
                                # Update corresponding German/English key
                                german_key_map = {
                                    'supplier_name': 'Lieferantename', 'company_name': 'Empfängerfirma',
                                    'invoice_number': 'Rechnungsnummer', 'invoice_date': 'Rechnungsdatum',
                                    'amount': 'Gesamtbetrag', 'vat_amount': 'Mehrwertsteuerbetrag',
                                    'description': 'Leistungsbeschreibung'
                                }
                                english_key_map = {v: k for k, v in german_key_map.items()}
                                
                                if target_field in german_key_map:
                                    form_data[german_key_map[target_field]] = val_flat
                                elif target_field in english_key_map:
                                    form_data[english_key_map[target_field]] = val_flat
                                break
                        if form_data[target_field]:
                            break

        # 4. Save to Pending
        current_source_info = {
            'source': source,
            'batch_id': batch_id,
            'processed_at': datetime.now().isoformat(),
            'filename': filename_orig,
            **(source_info or {})
        }
        
        pending_invoice_data = {
            **form_data,  # spread the form_data
            'original_path': file_path,  # original_path is the initially saved file path
            'needs_manual_input': not extracted_data.get('success', False) if isinstance(extracted_data, dict) else True,
            'extracted_data': json.dumps(extracted_data) if isinstance(extracted_data, dict) else json.dumps({'error': 'Failed to extract data'}),
            'source': source,
            'source_info': json.dumps(current_source_info),
            'batch_id': batch_id
        }

        try:
            pending_id = save_to_pending_func(pending_invoice_data, batch_id, current_source_info)
            form_data['pending_id'] = pending_id  # Add pending_id to the data returned to UI
            
            log.info(f"Data prepared for frontend (processing.py): {json.dumps(form_data, indent=2)}")

            return {
                'success': True,
                'filename': filename,
                'file_path': file_path,
                'preview_path': preview_path,
                'pending_id': pending_id,
                'extracted_data': form_data,  # This is the normalized data for the form
                'needs_validation': True,  # All uploads via this function require validation
                'status': 'processed'
            }
        except Exception as db_error:
            log.error(f"Database error saving pending invoice: {str(db_error)}", exc_info=True)
            return {
                'success': False,
                'filename': filename,
                'error': f"Database error: {str(db_error)}",
                'status': 'error'
            }

    except Exception as e:
        log.error(f"Error processing invoice file {filename_orig}: {str(e)}", exc_info=True)
        return {
            'success': False,
            'filename': filename_orig if 'filename_orig' in locals() else "unknown_file",
            'error': str(e),
            'status': 'error'
        }

# Placeholder for sanitize_filename if not using werkzeug.utils.secure_filename directly everywhere
# def sanitize_filename(filename):
#     # Basic sanitization, adapt as needed from your app.py logic
#     return re.sub(r'[^a-zA-Z0-9_.-]', '_', filename) 