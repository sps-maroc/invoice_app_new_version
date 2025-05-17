import os
import sqlite3
import logging
from datetime import datetime

# Setup logging
log = logging.getLogger(__name__)

def get_db_connection(db_path):
    """Create a database connection"""
    log.debug(f"Connecting to database at {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row  # Return rows as dictionaries
    return conn

def check_and_update_schema(conn=None, db_path=None):
    """Check and update database schema if needed"""
    close_conn = False
    if conn is None and db_path:
        conn = get_db_connection(db_path)
        close_conn = True
    
    try:
        # Check pending_invoices table for is_validated column
        cursor = conn.execute("PRAGMA table_info(pending_invoices)")
        columns = {col[1]: col for col in cursor.fetchall()}
        
        if 'is_validated' not in columns:
            log.info("Adding is_validated column to pending_invoices table")
            conn.execute("ALTER TABLE pending_invoices ADD COLUMN is_validated INTEGER DEFAULT 0")
            conn.commit()
        
        if 'validated_at' not in columns:
            log.info("Adding validated_at column to pending_invoices table")
            conn.execute("ALTER TABLE pending_invoices ADD COLUMN validated_at TEXT")
            conn.commit()
            
        if 'validated_by' not in columns:
            log.info("Adding validated_by column to pending_invoices table")
            conn.execute("ALTER TABLE pending_invoices ADD COLUMN validated_by TEXT")
            conn.commit()
        
        # Make sure batch_queue table exists
        conn.execute('''
        CREATE TABLE IF NOT EXISTS batch_queue (
            id INTEGER PRIMARY KEY,
            batch_id TEXT NOT NULL,
            file_path TEXT NOT NULL,
            preview_path TEXT,
            filename TEXT,
            status TEXT DEFAULT 'pending',
            processed_at TEXT,
            pending_id INTEGER,
            position INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        conn.commit()
        
        # Make sure email_credentials table exists
        conn.execute('''
        CREATE TABLE IF NOT EXISTS email_credentials (
            id INTEGER PRIMARY KEY,
            email TEXT UNIQUE,
            password TEXT,
            imap_server TEXT,
            port INTEGER DEFAULT 993,
            use_ssl INTEGER DEFAULT 1,
            is_custom INTEGER DEFAULT 0,
            custom_server TEXT,
            last_used TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        conn.commit()
        
        log.info("Database schema check and update completed successfully")
    except Exception as e:
        log.error(f"Error checking/updating schema: {e}")
        conn.rollback()
    finally:
        if close_conn:
            conn.close()

def initialize_shadow_table(db_path):
    """Initialize shadow copy of the pending_invoices table"""
    conn = get_db_connection(db_path)
    conn.execute('''
    CREATE TABLE IF NOT EXISTS pending_invoices (
        id INTEGER PRIMARY KEY,
        batch_id TEXT,
        file_path TEXT,
        original_path TEXT,
        preview_path TEXT,
        invoice_number TEXT,
        invoice_date TEXT,
        due_date TEXT,
        normalized_date TEXT,
        amount REAL,
        amount_original TEXT,
        vat_amount REAL,
        vat_amount_original TEXT,
        description TEXT,
        supplier_name TEXT,
        company_name TEXT,
        processed_at TEXT,
        is_viewed INTEGER DEFAULT 0,
        needs_manual_input INTEGER DEFAULT 0,
        is_finalized INTEGER DEFAULT 0,
        finalized_at TEXT,
        source TEXT,
        source_info TEXT,
        raw_text TEXT,
        ocr_text TEXT,
        extracted_data TEXT,
        validation_status TEXT,
        validation_notes TEXT,
        created_at TEXT,
        updated_at TEXT
    )
    ''')
    conn.commit()
    conn.close()

def initialize_tables(db_path):
    """Initialize database tables if they don't exist"""
    conn = get_db_connection(db_path)
    
    # Create invoices table
    conn.execute('''
    CREATE TABLE IF NOT EXISTS invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_path TEXT,
        original_path TEXT,
        invoice_number TEXT,
        invoice_date TEXT,
        due_date TEXT,
        normalized_date TEXT,
        amount REAL,
        vat_amount REAL,
        description TEXT,
        supplier_id INTEGER,
        company_id INTEGER,
        confidence REAL,
        ocr_text TEXT,
        raw_text TEXT,
        processed_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(supplier_id) REFERENCES suppliers(id),
        FOREIGN KEY(company_id) REFERENCES companies(id)
    )
    ''')
    
    # Create suppliers table
    conn.execute('''
    CREATE TABLE IF NOT EXISTS suppliers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE
    )
    ''')
    
    # Create companies table
    conn.execute('''
    CREATE TABLE IF NOT EXISTS companies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE
    )
    ''')
    
    # Create pending_invoices table
    conn.execute('''
    CREATE TABLE IF NOT EXISTS pending_invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT,
        file_path TEXT,
        original_path TEXT,
        preview_path TEXT,
        invoice_number TEXT,
        invoice_date TEXT,
        due_date TEXT,
        normalized_date TEXT,
        amount REAL,
        amount_original TEXT,
        vat_amount REAL,
        vat_amount_original TEXT,
        description TEXT,
        supplier_name TEXT,
        company_name TEXT,
        processed_at TEXT,
        is_viewed INTEGER DEFAULT 0,
        needs_manual_input INTEGER DEFAULT 0,
        is_finalized INTEGER DEFAULT 0,
        finalized_at TEXT,
        source TEXT,
        source_info TEXT,
        raw_text TEXT,
        ocr_text TEXT,
        extracted_data TEXT,
        validation_status TEXT,
        validation_notes TEXT,
        is_validated INTEGER DEFAULT 0,
        validated_at TEXT,
        validated_by TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT
    )
    ''')
    
    # Create batch_queue table
    conn.execute('''
    CREATE TABLE IF NOT EXISTS batch_queue (
        id INTEGER PRIMARY KEY,
        batch_id TEXT NOT NULL,
        file_path TEXT NOT NULL,
        preview_path TEXT,
        filename TEXT,
        status TEXT DEFAULT 'pending',
        processed_at TEXT,
        pending_id INTEGER,
        position INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    # Create lexoffice_credentials table
    conn.execute('''
    CREATE TABLE IF NOT EXISTS lexoffice_credentials (
        id INTEGER PRIMARY KEY,
        api_key TEXT NOT NULL,
        download_dir TEXT,
        is_active INTEGER DEFAULT 1,
        last_sync TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT
    )
    ''')
    
    # Create lexoffice_processed_vouchers table to track processed invoices
    conn.execute('''
    CREATE TABLE IF NOT EXISTS lexoffice_processed_vouchers (
        id INTEGER PRIMARY KEY,
        voucher_id TEXT UNIQUE,
        voucher_number TEXT,
        voucher_type TEXT,
        voucher_status TEXT,
        file_id TEXT,
        file_path TEXT,
        processed_at TEXT,
        batch_id TEXT,
        pending_id INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    # Create email_credentials table
    conn.execute('''
    CREATE TABLE IF NOT EXISTS email_credentials (
        id INTEGER PRIMARY KEY,
        email TEXT UNIQUE,
        password TEXT,
        imap_server TEXT,
        port INTEGER DEFAULT 993,
        use_ssl INTEGER DEFAULT 1,
        is_custom INTEGER DEFAULT 0,
        custom_server TEXT,
        last_used TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    # Check and add missing columns to tables
    check_and_update_schema(conn)
    
    conn.commit()
    conn.close()

def check_invoice_exists(invoice_number, db_path):
    """Check if an invoice with the given invoice number already exists in the database"""
    conn = get_db_connection(db_path)
    cursor = conn.execute("""
        SELECT COUNT(*) FROM invoices 
        WHERE invoice_number = ? OR invoice_number LIKE ? OR invoice_number LIKE ?
    """, (invoice_number, f"{invoice_number}%", f"%{invoice_number}"))
    count = cursor.fetchone()[0]
    conn.close()
    return count > 0

def save_to_pending(invoice_data, batch_id=None, source_info=None, db_path=None):
    """Save invoice data to pending_invoices table"""
    if batch_id is None:
        import uuid
        batch_id = str(uuid.uuid4())
        
    # Initialize database connection
    conn = get_db_connection(db_path)
    
    # Ensure invoice_data is a dictionary
    if invoice_data is None:
        invoice_data = {}
    
    # Make sure all required fields exist
    safe_data = {
        'file_path': invoice_data.get('file_path', ''),
        'original_path': invoice_data.get('original_path', invoice_data.get('file_path', '')),
        'preview_path': invoice_data.get('preview_path', ''),
        'invoice_number': invoice_data.get('Rechnungsnummer', invoice_data.get('invoice_number', '')),
        'invoice_date': invoice_data.get('Rechnungsdatum', invoice_data.get('invoice_date', '')),
        'due_date': invoice_data.get('Fälligkeitsdatum', invoice_data.get('due_date', '')),
        'amount': invoice_data.get('Gesamtbetrag', invoice_data.get('amount', '')),
        'vat_amount': invoice_data.get('Mehrwertsteuerbetrag', invoice_data.get('vat_amount', '')),
        'description': invoice_data.get('Leistungsbeschreibung', invoice_data.get('description', '')),
        'supplier_name': invoice_data.get('Lieferantename', invoice_data.get('supplier_name', '')),
        'company_name': invoice_data.get('Empfängerfirma', invoice_data.get('company_name', '')),
        'needs_manual_input': 1 if invoice_data.get('needs_manual_input', False) else 0,
        'validation_status': invoice_data.get('validation_status', 'pending'),
        'validation_notes': invoice_data.get('validation_notes', '')
    }
    
    # Convert any None values to empty strings to avoid database errors
    for key, value in safe_data.items():
        if value is None:
            safe_data[key] = ''
    
    # Get current time
    now = datetime.now().isoformat()
    
    # Extract extracted_data if available
    import json
    extracted_data = None
    if 'extracted_data' in invoice_data:
        if isinstance(invoice_data['extracted_data'], dict):
            extracted_data = json.dumps(invoice_data['extracted_data'])
        else:
            extracted_data = invoice_data['extracted_data']
    
    # Check if there's a data field in invoice_data
    if not extracted_data and isinstance(invoice_data.get('data', None), dict):
        extracted_data = json.dumps(invoice_data['data'])
    
    try:
        # First check if the columns we need exist in the database
        cursor = conn.execute("PRAGMA table_info(pending_invoices)")
        columns = {col[1]: col for col in cursor.fetchall()}
        
        # Create the base query and parameters
        fields = [
            "batch_id", "file_path", "original_path", "preview_path", 
            "invoice_number", "invoice_date", "due_date", "amount", "amount_original", 
            "vat_amount", "vat_amount_original", "description", "supplier_name", "company_name",
            "needs_manual_input", "validation_status", "validation_notes", "source", "source_info",
            "extracted_data", "created_at", "updated_at"
        ]
        
        values = [
            batch_id,
            safe_data['file_path'],
            safe_data['original_path'],
            safe_data['preview_path'],
            safe_data['invoice_number'],
            safe_data['invoice_date'],
            safe_data['due_date'],
            safe_data['amount'],
            safe_data['amount'],  # Store original amount string
            safe_data['vat_amount'],
            safe_data['vat_amount'],  # Store original VAT amount string
            safe_data['description'],
            safe_data['supplier_name'],
            safe_data['company_name'],
            safe_data['needs_manual_input'],
            safe_data['validation_status'],
            safe_data['validation_notes'],
            invoice_data.get('source', 'upload'),
            json.dumps(source_info) if source_info else '{}',
            extracted_data,
            now,
            now
        ]
        
        # Check if is_validated column exists and add it if so
        if 'is_validated' in columns:
            fields.append("is_validated")
            values.append(0)  # Default to not validated
            
        # Check if is_processed column exists
        if 'is_processed' in columns:
            fields.append("is_processed")
            values.append(0)  # Default to not processed
            
        # Build and execute the query
        query = f'''
        INSERT INTO pending_invoices (
            {', '.join(fields)}
        ) VALUES ({', '.join(['?'] * len(fields))})
        '''
        
        cursor = conn.execute(query, values)
        pending_id = cursor.lastrowid
        conn.commit()
        
        return pending_id
        
    except Exception as e:
        log.error(f"Error saving to pending: {str(e)}")
        conn.rollback()
        raise
    finally:
        conn.close()

def update_pending_invoice(invoice_id, data, db_path):
    """Update a pending invoice with new data"""
    try:
        conn = get_db_connection(db_path)
        
        # Get the existing invoice data
        cursor = conn.execute('SELECT * FROM pending_invoices WHERE id = ?', (invoice_id,))
        existing_invoice = cursor.fetchone()
        
        if not existing_invoice:
            conn.close()
            return False
        
        # Convert existing invoice to dict for easier access
        existing_data = dict(existing_invoice)
        
        # First check available columns in the table
        cursor = conn.execute("PRAGMA table_info(pending_invoices)")
        columns = {col[1]: col for col in cursor.fetchall()}
        
        # Build update query
        query_parts = []
        params = []
        
        # Set updated_at timestamp
        query_parts.append("updated_at = ?")
        params.append(datetime.now().isoformat())
        
        # Handle validation status if column exists
        if 'is_validated' in columns and 'is_validated' in data and data['is_validated']:
            query_parts.append("is_validated = ?")
            params.append(1)
            
            if 'validated_at' in columns:
                query_parts.append("validated_at = ?")
                params.append(datetime.now().isoformat())
            
        # Basic fields to update
        fields_to_update = [
            'invoice_number', 'invoice_date', 'due_date', 'amount', 
            'vat_amount', 'description', 'supplier_name', 'company_name',
            'validation_status', 'validation_notes', 'needs_manual_input'
        ]
        
        for field in fields_to_update:
            if field in data and data[field] is not None:
                query_parts.append(f"{field} = ?")
                params.append(data[field])
        
        # Only update if we have something to update
        if query_parts:
            query = f"UPDATE pending_invoices SET {', '.join(query_parts)} WHERE id = ?"
            params.append(invoice_id)
            
            cursor.execute(query, params)
            conn.commit()
            conn.close()
            return True
            
        conn.close()
        return False
        
    except Exception as e:
        log.error(f"Error updating pending invoice {invoice_id}: {str(e)}")
        try:
            conn.rollback()
            conn.close()
        except:
            pass
        return False 