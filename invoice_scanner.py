import os
import re
import json
import shutil
import argparse
import sqlite3
import glob
import fitz  # PyMuPDF
from datetime import datetime
from dateutil import parser
from pathlib import Path
import time
import logging
from langchain_ollama import OllamaLLM
from langchain_core.prompts import ChatPromptTemplate
from langchain_community.document_loaders import PyPDFLoader
import PyPDF2
import sys
import threading
from langchain_core.output_parsers import JsonOutputParser
from parser import InvoiceFields

# Configure module logger
log = logging.getLogger(__name__)

# Try importing pdf2image and pytesseract for OCR
try:
    from pdf2image import convert_from_path
    import pytesseract
    HAS_OCR_SUPPORT = True
    log.info("OCR support is available with pdf2image and pytesseract")
except ImportError:
    HAS_OCR_SUPPORT = False
    log.warning("OCR support is NOT available - install pdf2image and pytesseract for OCR functionality")

class InvoiceDatabase:
    """SQLite database manager for invoice data"""
    
    def __init__(self, db_path="invoices.db"):
        self.db_path = db_path
        self.conn = None
        self.cursor = None
        self.logger = logging.getLogger(f"{__name__}.InvoiceDatabase")
        self.initialize_db()
        
    def initialize_db(self):
        """Create the database and tables if they don't exist"""
        self.logger.info(f"Initializing database at {self.db_path}")
        self.conn = sqlite3.connect(self.db_path)
        self.cursor = self.conn.cursor()
        
        # Create tables
        self.cursor.execute('''
        CREATE TABLE IF NOT EXISTS suppliers (
            id INTEGER PRIMARY KEY,
            name TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        
        self.cursor.execute('''
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY,
            name TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        
        self.cursor.execute('''
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY,
            file_path TEXT UNIQUE,
            original_path TEXT,
            invoice_number TEXT,
            invoice_date TEXT,
            normalized_date TEXT,
            amount_original TEXT,
            vat_amount_original TEXT,
            description TEXT,
            supplier_id INTEGER,
            company_id INTEGER,
            processed_at TIMESTAMP,
            FOREIGN KEY (supplier_id) REFERENCES suppliers (id),
            FOREIGN KEY (company_id) REFERENCES companies (id)
        )
        ''')
        
        self.conn.commit()
        self.logger.debug("Database tables created successfully")
    
    def get_or_create_supplier(self, supplier_name):
        """Get supplier ID or create if it doesn't exist"""
        if not supplier_name or supplier_name == "Nicht gefunden":
            self.logger.debug("No valid supplier name provided")
            return None
            
        self.cursor.execute("SELECT id FROM suppliers WHERE name = ?", (supplier_name,))
        result = self.cursor.fetchone()
        
        if result:
            self.logger.debug(f"Found existing supplier: {supplier_name} (ID: {result[0]})")
            return result[0]
        else:
            self.cursor.execute("INSERT INTO suppliers (name) VALUES (?)", (supplier_name,))
            self.conn.commit()
            supplier_id = self.cursor.lastrowid
            self.logger.info(f"Created new supplier: {supplier_name} (ID: {supplier_id})")
            return supplier_id
    
    def get_or_create_company(self, company_name):
        """Get company ID or create if it doesn't exist"""
        if not company_name or company_name == "Nicht gefunden":
            company_name = "Unknown"
            self.logger.debug("Using 'Unknown' as company name")
            
        self.cursor.execute("SELECT id FROM companies WHERE name = ?", (company_name,))
        result = self.cursor.fetchone()
        
        if result:
            self.logger.debug(f"Found existing company: {company_name} (ID: {result[0]})")
            return result[0]
        else:
            self.cursor.execute("INSERT INTO companies (name) VALUES (?)", (company_name,))
            self.conn.commit()
            company_id = self.cursor.lastrowid
            self.logger.info(f"Created new company: {company_name} (ID: {company_id})")
            return company_id
    
    def store_invoice(self, invoice_data, file_path, original_path):
        """Store invoice information in the database"""
        self.logger.info(f"Storing invoice from {file_path} in database")
        
        # Extract data with safe defaults
        supplier_name = invoice_data.get('Lieferantename', 'Unknown')
        company_name = invoice_data.get('Empfängerfirma', 'Unknown')
        
        # Get or create related records
        supplier_id = self.get_or_create_supplier(supplier_name)
        company_id = self.get_or_create_company(company_name)
        
        # Parse and normalize the amount
        amount_original = invoice_data.get('Gesamtbetrag', '0')
        # Parse and normalize the VAT amount
        vat_original = invoice_data.get('Mehrwertsteuerbetrag', '0')
        # Parse and normalize the date
        date_original = invoice_data.get('Rechnungsdatum', None)
        normalized_date = self.normalize_date(date_original) if date_original else None
        
        # Insert into database
        try:
            self.cursor.execute('''
            INSERT INTO invoices (
                file_path, original_path, invoice_number, invoice_date, normalized_date,
                amount_original, vat_amount_original, description,
                supplier_id, company_id, processed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                file_path,
                original_path,
                invoice_data.get('Rechnungsnummer', 'Unknown'),
                date_original,
                normalized_date,
                amount_original,
                vat_original,
                invoice_data.get('Leistungsbeschreibung', ''),
                supplier_id,
                company_id,
                datetime.now().isoformat()


            ))
            self.conn.commit()
            self.logger.info(f"Invoice {invoice_data.get('Rechnungsnummer', 'Unknown')} successfully saved to database")
            return True
        except sqlite3.IntegrityError:
            # File path already exists in database
            self.logger.warning(f"Invoice already exists in database: {file_path}")
            return False
    
    def normalize_amount(self, amount_str):
        """Normalize amount strings to float values"""
        if not amount_str or amount_str == "Nicht gefunden":
            return 0.0
        
        try:
            # Remove currency symbols and non-numeric chars except for decimal points
            amount_str = re.sub(r'[^\d.,]', '', str(amount_str))
            
            # Replace comma with dot for decimal places (German format)
            if ',' in amount_str and '.' not in amount_str:
                amount_str = amount_str.replace(',', '.')
            elif ',' in amount_str and '.' in amount_str:
                # Handle German format with thousands separator (1.234,56)
                amount_str = amount_str.replace('.', '').replace(',', '.')
                
            return float(amount_str)
        except (ValueError, TypeError):
            self.logger.warning(f"Could not convert amount '{amount_str}' to float")
            return 0.0
    
    def normalize_date(self, date_str):
        """Normalize date strings to ISO format (YYYY-MM-DD)"""
        if not date_str or date_str == "Nicht gefunden":
            return None
        
        try:
            # Try to parse the date in various formats
            if re.match(r'\d{4}-\d{2}-\d{2}', date_str):
                date_obj = datetime.strptime(date_str, '%Y-%m-%d')
            elif re.match(r'\d{2}\.\d{2}\.\d{4}', date_str):
                date_obj = datetime.strptime(date_str, '%d.%m.%Y')
            else:
                # Try generic parsing
                date_obj = parser.parse(date_str)
            
            # Return in ISO format
            return date_obj.strftime('%Y-%m-%d')
        except Exception as e:
            self.logger.error(f"Error parsing date '{date_str}': {str(e)}")
            return None
    
    def close(self):
        """Close the database connection"""
        if self.conn:
            self.conn.close()


class InvoiceScanner:
    """Invoice scanner and organizer using LLM for data extraction"""
    
    def __init__(self, db_path="invoices.db", archive_dir="organized_invoices"):
        self.db_path = db_path
        self.archive_dir = archive_dir
        self.db = InvoiceDatabase(db_path)
        self.logger = logging.getLogger(f"{__name__}.InvoiceScanner")
        
        # Initialize directories
        os.makedirs(self.archive_dir, exist_ok=True)
        os.makedirs(os.path.join(self.archive_dir, "by_date"), exist_ok=True)
        os.makedirs(os.path.join(self.archive_dir, "by_supplier"), exist_ok=True)
        
        # Initialize Ollama LLM
        OLLAMA_HOST = os.environ.get('OLLAMA_HOST', 'http://localhost:11434')
        self.model_name = 'llama3.2:latest'
        self.logger.info(f"Using Ollama model {self.model_name} at {OLLAMA_HOST}")
        
        # Set up skipped invoices tracking
        self.skipped_invoices = []
        
        # Define the system prompt template for invoice extraction
        self.invoice_template = """
Du bist ein spezialisierter KI-Assistent für die Extraktion von Daten aus deutschen Geschäftsrechnungen.
Deine Aufgabe ist es, wichtige Informationen aus dem Text der Rechnung zu extrahieren und in einem strukturierten JSON-Format zurückzugeben.

INVOICE TEXT:
{invoice_text}

Extrahiere folgende Informationen aus der Rechnung und gib sie in JSON-Format zurück:
- Lieferantename (Name des Unternehmens, das die Rechnung ausgestellt hat)
- Rechnungsdatum (im Format DD.MM.YYYY oder YYYY-MM-DD)
- Gesamtbetrag (mit Währung, z.B. "1.234,56 EUR")
- Empfängerfirma (Firma, die die Rechnung erhalten hat)
- Rechnungsnummer
- Mehrwertsteuerbetrag (mit Währung)
- Leistungsbeschreibung (eine kurze Beschreibung der Waren oder Dienstleistungen)

Bemerkungen:
1. Gib nur die gefundenen Werte im JSON-Format zurück, keine Erklärungen.
2. Falls eine Information nicht in der Rechnung zu finden ist, gib einen leeren String zurück.
3. Achte besonders auf das korrekte Format von Datum und Beträgen.
"""
        self.logger.debug("Invoice scanner initialized")
    
    def extract_text_from_pdf(self, file_path):
        """Extract text from PDF file using PyMuPDF"""
        self.logger.info(f"Extracting text from {file_path}")
        text = ""
        ocr_text = ""
        
        # Check if file exists
        if not os.path.exists(file_path):
            self.logger.error(f"File not found: {file_path}")
            return "", ""
        
        try:
            # Extract text using PyMuPDF
            doc = fitz.open(file_path)
            
            for page in doc:
                text += page.get_text()
            
            # If no text was extracted and OCR is available, try OCR
            if not text.strip() and HAS_OCR_SUPPORT:
                self.logger.info(f"No text found in PDF, attempting OCR: {file_path}")
                try:
                    # Convert PDF to images
                    images = convert_from_path(file_path)
                    
                    for i, image in enumerate(images):
                        # Perform OCR on each page
                        page_text = pytesseract.image_to_string(image, lang='deu+eng')
                        ocr_text += f"\n--- Page {i+1} ---\n{page_text}"
                    
                    self.logger.info(f"OCR completed for {file_path}")
                    
                    # If OCR found text, use it
                    if ocr_text.strip():
                        text = ocr_text
                except Exception as e:
                    self.logger.error(f"OCR failed: {str(e)}")
            
            doc.close()
            
            # Check if this is a document containing "Rechnung"
            combined_text = (text + " " + ocr_text).lower()
            if "rechnung" not in combined_text and "invoice" not in combined_text and "faktura" not in combined_text:
                self.logger.info(f"Document does not contain invoice keywords - skipping processing: {file_path}")
                self.skipped_invoices.append({
                    "file_path": file_path,
                    "reason": "Does not contain invoice keywords",
                    "timestamp": datetime.now().isoformat()
                })
                return "SKIP_PROCESSING", ocr_text
                
            return text, ocr_text
        except Exception as e:
            self.logger.error(f"Error extracting text from PDF: {str(e)}")
            return "", ""
    
    def process_with_model(self, text, model_name=None):
        """Process invoice text with a specific LLM model"""
        # Skip processing if text is marked to skip
        if text == "SKIP_PROCESSING":
            return {
                "success": False,
                "error": "Document does not appear to be an invoice",
                "skipped": True
            }
        try:
            # Always use llama3.2:latest
            effective_model_name = 'llama3.2:latest'
            OLLAMA_HOST = os.environ.get('OLLAMA_HOST', 'http://localhost:11434')
            
            # Use fixed settings for llama3.2:latest
            timeout = 360  # 6 minutes
            temperature = 0.15
            self.logger.info(f"Using llama3.2:latest model with temperature={temperature}, timeout={timeout}s")
            
            # Create the LLM with the appropriate settings
            llm = OllamaLLM(
                model=effective_model_name,
                temperature=temperature
            )
            
            # Directly create the full prompt text without using ChatPromptTemplate
            # This avoids variable escaping issues with curly braces
            
            # Create a simplified prompt with explicit JSON instructions - ALL IN GERMAN
            system_prompt = self.invoice_template + """
            Bitte extrahiere folgende Informationen aus der deutschen Rechnung in genau diesem JSON-Format:

            ```json
            {
                "Lieferantename": "Name des Unternehmens, das die Rechnung ausgestellt hat",
                "Rechnungsdatum": "Datum im Format DD.MM.YYYY oder YYYY-MM-DD",
                "Gesamtbetrag": "Betrag mit Währung, z.B. 1.234,56 EUR",
                "Empfängerfirma": "Name des Empfängerunternehmens",
                "Rechnungsnummer": "Rechnungsnummer/Kennung",
                "Mehrwertsteuerbetrag": "MwSt-Betrag mit Währung",
                "Leistungsbeschreibung": "Beschreibung der Waren oder Dienstleistungen"
            }
            ```

            Wichtige Hinweise für die Extraktion:
            1. Achte besonders auf die korrekte Extraktion von deutschen Datumsformaten (oft als TT.MM.JJJJ).
            2. Bei Geldbeträgen beachte das deutsche Format (Komma als Dezimaltrennzeichen, z.B. 1.234,56 €).
            3. Die Mehrwertsteuer kann als "MwSt.", "USt.", "Umsatzsteuer", oder "19%" gekennzeichnet sein.
            4. Die Rechnung kann "Rechnung", "Faktura" oder "Invoice" genannt werden.
            5. Falls eine Information nicht gefunden werden kann, gib "Nicht gefunden" zurück.

            Gib NUR das JSON zurück, ohne zusätzlichen Text davor oder danach.
            """
            
            full_prompt = f"{system_prompt}\n\nINVOICE TEXT:\n{text}"
            
            # Invoke the LLM directly
            start_time = time.time()
            self.logger.info(f"Invoking {effective_model_name} model (timeout: {timeout}s)...")
            
            # Use a thread-safe approach for timeout handling
            result = None
            error = None
            
            def run_model():
                nonlocal result, error
                try:
                    self.logger.debug(f"Starting model inference with {effective_model_name}")
                    result = llm.invoke(full_prompt)
                except Exception as e:
                    error = e
                    self.logger.error(f"Error in model thread: {str(e)}")
            
            # Create and start thread
            thread = threading.Thread(target=run_model)
            thread.daemon = True
            self.logger.debug(f"Starting model thread for {effective_model_name}")
            thread.start()
            
            # Wait for completion or timeout
            thread.join(timeout=timeout)
            
            # Check if thread is still alive (timeout occurred)
            if thread.is_alive():
                self.logger.warning(f"Model {effective_model_name} timed out after {timeout} seconds")
                return {
                    "Lieferantename": "Not available - AI timeout",
                    "Rechnungsdatum": "",
                    "Gesamtbetrag": "0",
                    "Empfängerfirma": "",
                    "Rechnungsnummer": "",
                    "Mehrwertsteuerbetrag": "0",
                    "Leistungsbeschreibung": "AI processing timed out. Please input data manually.",
                    "success": False,
                    "error": f"Model processing timed out after {timeout} seconds"
                }
            
            # If there was an error in the thread, return structured error data
            if error:
                self.logger.error(f"Model error: {str(error)}")
                return {
                    "Lieferantename": "Not available - AI error",
                    "Rechnungsdatum": "",
                    "Gesamtbetrag": "0",
                    "Empfängerfirma": "",
                    "Rechnungsnummer": "",
                    "Mehrwertsteuerbetrag": "0",
                    "Leistungsbeschreibung": "AI processing error. Please input data manually.",
                    "success": False,
                    "error": f"Error during model processing: {str(error)}"
                }
            
            if not result:
                self.logger.error(f"No result returned from {effective_model_name}")
                return {
                    "Lieferantename": "Not available - No result",
                    "Rechnungsdatum": "",
                    "Gesamtbetrag": "0",
                    "Empfängerfirma": "",
                    "Rechnungsnummer": "",
                    "Mehrwertsteuerbetrag": "0",
                    "Leistungsbeschreibung": "AI returned no result. Please input data manually.",
                    "success": False,
                    "error": "No result returned from model"
                }
            
            # Parse the JSON response manually with error handling
            try:
                # Extract JSON if it's wrapped in markdown code blocks
                json_text = result
                if "```json" in json_text:
                    json_text = json_text.split("```json")[1].split("```")[0].strip()
                elif "```" in json_text:
                    json_text = json_text.split("```")[1].split("```")[0].strip()
                
                # Parse the JSON
                data = json.loads(json_text)
                
                # Add success flag
                data["success"] = True
                
                # Calculate processing time
                processing_time = time.time() - start_time
                self.logger.info(f"{effective_model_name} processing successful in {processing_time:.2f}s")
                
                return data
            except json.JSONDecodeError as e:
                self.logger.error(f"Failed to parse JSON from {effective_model_name}: {e}")
                self.logger.debug(f"Raw response: {result}")
                
                return {
                    "Lieferantename": "Not available - JSON parsing error",
                    "Rechnungsdatum": "",
                    "Gesamtbetrag": "0",
                    "Empfängerfirma": "",
                    "Rechnungsnummer": "",
                    "Mehrwertsteuerbetrag": "0",
                    "Leistungsbeschreibung": "Error parsing AI result. Please input data manually.",
                    "success": False,
                    "error": f"Failed to parse JSON: {str(e)}"
                }
                
        except Exception as e:
            # Handle exceptions including timeout
            self.logger.error(f"Error with {effective_model_name}: {str(e)}")
            
            return {
                "Lieferantename": "Not available - Processing error",
                "Rechnungsdatum": "",
                "Gesamtbetrag": "0",
                "Empfängerfirma": "",
                "Rechnungsnummer": "",
                "Mehrwertsteuerbetrag": "0",
                "Leistungsbeschreibung": "Error processing invoice. Please input data manually.",
                "success": False,
                "error": str(e)
            }
    
    def organize_file(self, file_path, data):
        """Organize a file into the archive directory with proper structure"""
        try:
            # Extract necessary information
            company_name = data.get('Empfängerfirma', 'Unknown')
            invoice_date = data.get('Rechnungsdatum')
            invoice_number = data.get('Rechnungsnummer', '')
            supplier_name = data.get('Lieferantename', 'Unknown')
            
            # Clean company name for folder name
            company_folder = re.sub(r'[^\w\s-]', '', company_name).strip()
            if not company_folder:
                company_folder = 'Unknown'
                
            # Clean supplier name for folder name
            supplier_folder = re.sub(r'[^\w\s-]', '', supplier_name).strip()
            if not supplier_folder:
                supplier_folder = 'Unknown'
            
            # Parse date for folder structure
            try:
                if invoice_date:
                    # Try to parse the date
                    if re.match(r'\d{4}-\d{2}-\d{2}', invoice_date):
                        date_obj = datetime.strptime(invoice_date, '%Y-%m-%d')
                    elif re.match(r'\d{2}\.\d{2}\.\d{4}', invoice_date):
                        date_obj = datetime.strptime(invoice_date, '%d.%m.%Y')
                    else:
                        # Try generic parsing
                        date_obj = parser.parse(invoice_date)
                    
                    # Extract year and month
                    year = date_obj.strftime('%Y')
                    month = date_obj.strftime('%m')
                else:
                    # Use current date if invoice date is not available
                    now = datetime.now()
                    year = now.strftime('%Y')
                    month = now.strftime('%m')
            except Exception as e:
                # Use current date if parsing fails
                self.logger.error(f"Error parsing date '{invoice_date}': {str(e)}")
                now = datetime.now()
                year = now.strftime('%Y')
                month = now.strftime('%m')
            
            # Create directory structure using the new format
            # organized_invoices/by_company/{company_name}/by_date/{year}/{month}/{supplier}/{invoice_number}.pdf
            by_company_dir = os.path.join(self.archive_dir, "by_company")
            company_path = os.path.join(by_company_dir, company_folder)
            by_date_dir = os.path.join(company_path, "by_date")
            year_path = os.path.join(by_date_dir, year)
            month_path = os.path.join(year_path, month)
            supplier_path = os.path.join(month_path, supplier_folder)
            
            # Create directories if they don't exist
            os.makedirs(supplier_path, exist_ok=True)
            
            # Generate unique filename
            base_name = os.path.splitext(os.path.basename(file_path))[0]
            if invoice_number:
                # Clean invoice number for filename
                clean_invoice_number = re.sub(r'[^\w-]', '_', invoice_number)
                new_filename = f"{clean_invoice_number}.pdf"
            else:
                # Use timestamp if no invoice number
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                new_filename = f"{timestamp}_{base_name}.pdf"
            
            # Ensure filename is unique
            target_path = os.path.join(supplier_path, new_filename)
            counter = 1
            while os.path.exists(target_path):
                name_parts = os.path.splitext(new_filename)
                new_filename = f"{name_parts[0]}_{counter}{name_parts[1]}"
                target_path = os.path.join(supplier_path, new_filename)
                counter += 1
            
            # Copy file to new location
            shutil.copy2(file_path, target_path)
            self.logger.info(f"Organized file to: {target_path}")
            
            return target_path
            
        except Exception as e:
            self.logger.error(f"Error organizing file: {e}")
            return None

    def process_invoice(self, file_path, model_name=None, store_in_db=True):
        """Process a single invoice file"""
        self.logger.info(f"Processing invoice: {file_path}")
        
        # Use specified model or default
        model_to_use = model_name or self.model_name
        
        try:
            # Extract text from PDF
            text, ocr_text = self.extract_text_from_pdf(file_path)
            
            # Check if this file should be skipped
            if text == "SKIP_PROCESSING":
                self.logger.info(f"Skipping processing for {file_path}: Does not contain 'Rechnung'")
                return {
                    "status": "skipped", 
                    "reason": "Does not contain 'Rechnung'", 
                    "file_path": file_path,
                    "success": False,
                    "error": "Document does not contain 'Rechnung' - skipping processing",
                    "ocr_text": ocr_text
                }
            
            # Check if there is any text to process
            if not text.strip():
                self.logger.warning(f"No text extracted from {file_path}")
                return {"status": "error", "error": "No text could be extracted from PDF", "success": False}
            
            # Extract data using LLM
            invoice_data = self.process_with_model(text, model_to_use)
            
            if not invoice_data:
                self.logger.warning(f"Failed to extract invoice data from {file_path}")
                return {"status": "error", "error": "Failed to extract invoice data", "success": False}
            
            # Normalize field names for consistency
            standard_fields = {
                'supplier_name': invoice_data.get('Lieferantename', '') or invoice_data.get('supplier_name', ''),
                'company_name': invoice_data.get('Empfängerfirma', '') or invoice_data.get('company_name', ''),
                'invoice_number': invoice_data.get('Rechnungsnummer', '') or invoice_data.get('invoice_number', ''),
                'invoice_date': invoice_data.get('Rechnungsdatum', '') or invoice_data.get('invoice_date', ''),
                'amount': invoice_data.get('Gesamtbetrag', '') or invoice_data.get('amount', ''),
                'vat_amount': invoice_data.get('Mehrwertsteuerbetrag', '') or invoice_data.get('vat_amount', ''),
                'description': invoice_data.get('Leistungsbeschreibung', '') or invoice_data.get('description', '')
            }
            
            # Add standardized fields while keeping original fields too
            for key, value in standard_fields.items():
                if value:  # Only add non-empty values
                    invoice_data[key] = value
            
            # Organize the file
            organized_path = self.organize_file(file_path, invoice_data)
            
            # Store in database
            if store_in_db and organized_path:
                success = self.db.store_invoice(invoice_data, organized_path, file_path)
                if success:
                    self.logger.info(f"Invoice {invoice_data.get('Rechnungsnummer', 'Unknown')} processed and stored")
                    return {
                        "status": "success",
                        "success": True,
                        "invoice_data": invoice_data,
                        "original_path": file_path,
                        "organized_path": organized_path,
                        "model_used": model_to_use
                    }
                else:
                    self.logger.warning(f"Invoice already exists in database: {file_path}")
                    return {
                        "status": "duplicate", 
                        "error": "Invoice already exists in database", 
                        "file_path": file_path,
                        "success": False,
                        "is_duplicate": True
                    }
            else:
                self.logger.error(f"Failed to organize file: {file_path}")
                return {"status": "error", "error": "Failed to organize file", "success": False}
                
        except Exception as e:
            self.logger.error(f"Error processing invoice {file_path}: {str(e)}")
            return {"status": "error", "error": str(e), "file_path": file_path, "success": False}
    
    def process_directory(self, directory, output_file=None, recursive=False):
        """Process all PDF files in a directory"""
        self.logger.info(f"Processing directory: {directory}, recursive={recursive}")
        
        pattern = os.path.join(directory, "**/*.pdf") if recursive else os.path.join(directory, "*.pdf")
        files = glob.glob(pattern, recursive=recursive)
        
        results = {
            "processed": 0,
            "skipped": 0,
            "errors": 0,
            "duplicates": 0,
            "files": []
        }
        
        for file in files:
            self.logger.info(f"Processing file: {file}")
            result = self.process_invoice(file)
            
            # Add to results
            if result["status"] == "success":
                results["processed"] += 1
                results["files"].append({"file": file, "status": "success", "data": result["invoice_data"]})
            elif result["status"] == "skipped":
                results["skipped"] += 1
                results["files"].append({"file": file, "status": "skipped", "reason": result["reason"]})
            elif result["status"] == "duplicate":
                results["duplicates"] += 1
                results["files"].append({"file": file, "status": "duplicate"})
            else:
                results["errors"] += 1
                results["files"].append({"file": file, "status": "error", "error": result.get("error", "Unknown error")})
        
        # Write results to output file if specified
        if output_file:
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(results, f, indent=4, ensure_ascii=False)
                
        # Log skipped invoices for reference
        if self.skipped_invoices:
            skipped_log_file = os.path.join(os.path.dirname(self.db_path), "skipped_invoices.json")
            try:
                with open(skipped_log_file, 'w', encoding='utf-8') as f:
                    json.dump(self.skipped_invoices, f, indent=4, ensure_ascii=False)
                self.logger.info(f"Logged {len(self.skipped_invoices)} skipped invoices to {skipped_log_file}")
            except Exception as e:
                self.logger.error(f"Failed to write skipped invoices log: {str(e)}")
        
        self.logger.info(f"Directory processing complete. Processed: {results['processed']}, "
                         f"Skipped: {results['skipped']}, "
                         f"Errors: {results['errors']}, "
                         f"Duplicates: {results['duplicates']}")
                         
        return results
    
    def close(self):
        """Close database connection"""
        self.logger.debug("Closing database connection")
        self.db.close()

    def extract_invoice_data(self, file_path, model_name=None, store_in_db=True):
        """Extract invoice data from a PDF file"""
        try:
            # Extract text from PDF
            text, ocr_text = self.extract_text_from_pdf(file_path)
            
            # Skip processing if text is marked to skip
            if text == "SKIP_PROCESSING":
                return {
                    "success": False,
                    "error": "Document does not appear to be an invoice",
                    "skipped": True
                }
            
            # If model_name not specified, select one based on file size
            if not model_name:
                file_size = os.path.getsize(file_path)
                from utils.ai_utils import select_ai_model
                model_name = select_ai_model(file_path, file_size)
            
            # Process the text with model
            result = self.process_with_model(text, model_name)
            
            # Normalize field names for consistency
            standard_fields = {
                'supplier_name': result.get('Lieferantename', '') or result.get('supplier_name', ''),
                'company_name': result.get('Empfängerfirma', '') or result.get('company_name', ''),
                'invoice_number': result.get('Rechnungsnummer', '') or result.get('invoice_number', ''),
                'invoice_date': result.get('Rechnungsdatum', '') or result.get('invoice_date', ''),
                'amount': result.get('Gesamtbetrag', '') or result.get('amount', ''),
                'vat_amount': result.get('Mehrwertsteuerbetrag', '') or result.get('vat_amount', ''),
                'description': result.get('Leistungsbeschreibung', '') or result.get('description', '')
            }
            
            # Add standardized fields while keeping original fields too
            for key, value in standard_fields.items():
                if value:  # Only add non-empty values
                    result[key] = value
            
            # Only organize and store in db if explicitly requested and processing was successful
            # This step is skipped during initial validation
            if store_in_db and result.get('success', False):
                original_path = file_path
                # Organize the file
                archive_path = self.organize_file(file_path, result)
                
                if archive_path:
                    # Store in database
                    self.db.store_invoice(result, archive_path, original_path)
                    self.logger.info(f"Stored invoice from {archive_path} in database")
                    if 'invoice_number' in result:
                        self.logger.info(f"Invoice {result['invoice_number']} successfully saved to database")
                    
                    # Add the archive path to the result
                    result['archive_path'] = archive_path
            
            # For validation-only calls, include the invoice number in the response for better error messages
            if not store_in_db:
                if 'Rechnungsnummer' in result:
                    result['invoice_number'] = result['Rechnungsnummer']
            
            return result
        except Exception as e:
            self.logger.error(f"Error extracting invoice data: {str(e)}")
            return {
                "success": False,
                "error": str(e)
            }

def main():
    """This function is deprecated - use cli.py instead"""
    import sys
    from cli import main as cli_main
    
    logging.warning("The main() function in invoice_scanner.py is deprecated. Please use cli.py instead.")
    return cli_main()

if __name__ == "__main__":
    sys.exit(main()) 