import imaplib
import email
import socket
import logging
import os
import uuid
import re
import json
import time
import mimetypes
import functools
import shutil
from datetime import datetime
from email.header import decode_header
import sqlite3

# Setup logging
log = logging.getLogger(__name__)

# Store active email connections
email_connections = {}

def imap_connection_wrapper(func):
    """Decorator to handle common IMAP connection errors"""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            # Set socket timeout to prevent hanging connections
            socket.setdefaulttimeout(30)  # 30 seconds timeout
            return func(*args, **kwargs)
        except imaplib.IMAP4.error as e:
            log.error(f"IMAP error in {func.__name__}: {str(e)}")
            error_msg = str(e).lower()
            
            # Check if the error contains binary data (corrupted response)
            if isinstance(e.args[0], bytes) or "\\x" in str(e) or "unexpected response" in error_msg:
                log.error(f"Binary/corrupted response detected in {func.__name__}")
                # Reset the connection and return appropriate message
                try:
                    # Try to get the session ID from the request
                    if 'session_id' in kwargs:
                        session_id = kwargs['session_id']
                        if session_id in email_connections:
                            # Try to close and reconnect
                            try:
                                mail = email_connections[session_id]['connection']
                                mail.close()
                                mail.logout()
                            except:
                                pass  # Ignore errors during cleanup
                            # Remove the corrupted connection
                            del email_connections[session_id]
                except:
                    pass  # Ignore errors during cleanup attempt
                    
                return {
                    'success': False, 
                    'error': 'Connection corrupted. Please reconnect to the mail server.',
                    'reconnect_required': True
                }, 503
            
            if "timeout" in error_msg:
                return {
                    'success': False, 
                    'error': 'Connection timed out. Please try again.',
                    'reconnect_required': True
                }, 504
            elif "login" in error_msg or "authentication" in error_msg:
                return {
                    'success': False, 
                    'error': 'Login failed. Please check your credentials.'
                }, 401
            elif "connection" in error_msg:
                return {
                    'success': False, 
                    'error': 'Failed to connect to the mail server. Please check your server settings and try again.',
                    'reconnect_required': True
                }, 503
            else:
                return {
                    'success': False, 
                    'error': f'IMAP error: {str(e)}'
                }, 500
        except socket.timeout:
            return {
                'success': False, 
                'error': 'Connection timed out. Please try again later.',
                'reconnect_required': True
            }, 504
        except socket.error as socket_error:
            error_code = getattr(socket_error, 'errno', None)
            error_msg = str(socket_error).lower()
            
            if error_code == 10061 or "connection refused" in error_msg:
                return {
                    'success': False,
                    'error': 'Connection refused. Please check your server address, port, and ensure the server is accepting connections.',
                    'reconnect_required': True
                }, 503
            elif error_code == 10060 or "timed out" in error_msg:
                return {
                    'success': False,
                    'error': 'Connection timed out. The server took too long to respond.',
                    'reconnect_required': True
                }, 504
            elif "connection reset" in error_msg or error_code == 10054:
                return {
                    'success': False,
                    'error': 'Connection reset by server. This might indicate server overload or network issues.',
                    'reconnect_required': True
                }, 503
            else:
                return {
                    'success': False,
                    'error': f'Socket error ({error_code}): {error_msg}',
                    'reconnect_required': True
                }, 503
        except ConnectionRefusedError:
            return {
                'success': False,
                'error': 'Connection refused. Please check server address and port.',
                'reconnect_required': True
            }, 503
        except Exception as e:
            log.error(f"Error in {func.__name__}: {str(e)}")
            # Check if it's a socket or connection related error
            error_str = str(e).lower()
            if "socket" in error_str or "connect" in error_str or "timeout" in error_str:
                return {
                    'success': False, 
                    'error': 'Connection issue. Please try again later.',
                    'reconnect_required': True
                }, 503
            return {
                'success': False, 
                'error': str(e)
            }, 500
    return wrapper

def save_email_credentials(email, password, imap_server, port=993, use_ssl=True, is_custom=False, custom_server=None, db_connection=None):
    """Save email credentials to the database"""
    try:
        conn = db_connection
        cursor = conn.cursor()
        
        # Check if this email already exists
        cursor.execute("SELECT id FROM email_credentials WHERE email = ?", (email,))
        existing = cursor.fetchone()
        
        # Get the current time
        current_time = datetime.now().isoformat()
        
        # Check if the last_used column exists
        columns_exist = True
        try:
            cursor.execute("SELECT last_used FROM email_credentials LIMIT 1")
        except sqlite3.OperationalError:
            columns_exist = False
            # Add the column if it doesn't exist
            try:
                cursor.execute("ALTER TABLE email_credentials ADD COLUMN last_used TEXT")
                conn.commit()
                log.info("Added last_used column to email_credentials table")
                columns_exist = True
            except Exception as column_err:
                log.error(f"Error adding last_used column: {str(column_err)}")
        
        if existing:
            # Update existing record
            if columns_exist:
                cursor.execute('''
                    UPDATE email_credentials SET 
                        password = ?, 
                        imap_server = ?, 
                        port = ?, 
                        use_ssl = ?, 
                        is_custom = ?, 
                        custom_server = ?,
                        last_used = ?
                    WHERE email = ?
                ''', (
                    password, 
                    imap_server, 
                    port, 
                    1 if use_ssl else 0, 
                    1 if is_custom else 0, 
                    custom_server,
                    current_time,
                    email
                ))
            else:
                # Fallback without last_used column
                cursor.execute('''
                    UPDATE email_credentials SET 
                        password = ?, 
                        imap_server = ?, 
                        port = ?, 
                        use_ssl = ?, 
                        is_custom = ?, 
                        custom_server = ?
                    WHERE email = ?
                ''', (
                    password, 
                    imap_server, 
                    port, 
                    1 if use_ssl else 0, 
                    1 if is_custom else 0, 
                    custom_server,
                    email
                ))
        else:
            # Insert new record
            if columns_exist:
                cursor.execute('''
                    INSERT INTO email_credentials (
                        email, password, imap_server, port, use_ssl, is_custom, custom_server, last_used, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    email, 
                    password, 
                    imap_server, 
                    port, 
                    1 if use_ssl else 0, 
                    1 if is_custom else 0, 
                    custom_server,
                    current_time,
                    current_time
                ))
            else:
                # Fallback without last_used column
                cursor.execute('''
                    INSERT INTO email_credentials (
                        email, password, imap_server, port, use_ssl, is_custom, custom_server, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    email, 
                    password, 
                    imap_server, 
                    port, 
                    1 if use_ssl else 0, 
                    1 if is_custom else 0, 
                    custom_server,
                    current_time
                ))
        
        conn.commit()
        return True, cursor.lastrowid if not existing else existing[0]
    except Exception as e:
        log.error(f"Error saving email credentials: {str(e)}")
        return False, str(e)

def get_email_credentials(email=None, credential_id=None, db_connection=None):
    """Get email credentials from the database"""
    try:
        conn = db_connection
        cursor = conn.cursor()
        
        if email:
            cursor.execute("SELECT * FROM email_credentials WHERE email = ?", (email,))
        elif credential_id:
            cursor.execute("SELECT * FROM email_credentials WHERE id = ?", (credential_id,))
        else:
            cursor.execute("SELECT * FROM email_credentials ORDER BY last_used DESC")
        
        columns = [column[0] for column in cursor.description]
        
        if email or credential_id:
            row = cursor.fetchone()
            if not row:
                return None
            
            result = dict(zip(columns, row))
            # Convert integer booleans to Python booleans
            result['use_ssl'] = bool(result['use_ssl'])
            result['is_custom'] = bool(result['is_custom'])
            return result
        else:
            # Return all credentials
            results = []
            for row in cursor.fetchall():
                result = dict(zip(columns, row))
                # Convert integer booleans to Python booleans
                result['use_ssl'] = bool(result['use_ssl'])
                result['is_custom'] = bool(result['is_custom'])
                # Don't include password in list view
                result['password'] = '••••••••'
                results.append(result)
            return results
    except Exception as e:
        log.error(f"Error getting email credentials: {str(e)}")
        return None

def delete_email_credentials(email=None, credential_id=None, db_connection=None):
    """Delete email credentials from the database"""
    try:
        conn = db_connection
        cursor = conn.cursor()
        
        if email:
            cursor.execute("DELETE FROM email_credentials WHERE email = ?", (email,))
        elif credential_id:
            cursor.execute("DELETE FROM email_credentials WHERE id = ?", (credential_id,))
        else:
            return False
        
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        log.error(f"Error deleting email credentials: {str(e)}")
        return False

def cleanup_expired_connections(email_session_timeout):
    """Function to clean up expired email connections"""
    now = datetime.now()
    expired_sessions = []
    
    for session_id, session_data in email_connections.items():
        created_at = session_data.get('created_at')
        if created_at and (now - created_at).total_seconds() > email_session_timeout:
            expired_sessions.append(session_id)
    
    for session_id in expired_sessions:
        try:
            mail = email_connections[session_id]['connection']
            try:
                mail.close()
            except:
                pass
                
            try:
                mail.logout()
            except:
                pass
            
            del email_connections[session_id]
            log.info(f"Cleaned up expired email session: {session_id}")
        except Exception as e:
            log.error(f"Error cleaning up expired session {session_id}: {str(e)}")
            
            # Still remove it from the dictionary
            if session_id in email_connections:
                del email_connections[session_id]

def cleanup_email_attachments(file_paths):
    """Clean up temporary email attachments"""
    for path in file_paths:
        try:
            if os.path.exists(path):
                os.remove(path)
                log.info(f"Cleaned up email attachment: {path}")
        except Exception as e:
            log.error(f"Failed to clean up attachment {path}: {str(e)}")

def connect_to_email(email_addr, password, imap_server, port=993, use_ssl=True, custom_server=None):
    """Connect to email server"""
    server_host = custom_server if imap_server == 'custom' else imap_server
    
    # Define well-known server settings for better connection handling
    server_settings = {
        'imap.gmail.com': {
            'timeout': 60,  # Increased timeout for Gmail
            'retry_count': 3,
            'ssl': True,
            'default_port': 993,
        },
        'outlook.office365.com': {
            'timeout': 90,  # Office365 can be slower
            'retry_count': 3,
            'ssl': True,
            'default_port': 993,
        },
        'imap.mail.yahoo.com': {
            'timeout': 60,  # Increased timeout for Yahoo
            'retry_count': 3,
            'ssl': True,
            'default_port': 993,
        }
    }
    
    # Get server settings or use defaults for custom/unknown servers
    settings = server_settings.get(server_host, {
        'timeout': 60,  # Increased default timeout
        'retry_count': 3,
        'ssl': use_ssl,
        'default_port': port
    })
    
    # Set socket timeout based on server settings
    socket.setdefaulttimeout(settings['timeout'])
    
    # Try to connect with retries
    last_exception = None
    for attempt in range(settings['retry_count']):
        try:
            log.info(f"Connecting to {server_host}:{port} (attempt {attempt+1}/{settings['retry_count']})")
            
            # Implement exponential backoff for retries after first attempt
            if attempt > 0:
                delay = min(30, 2 ** attempt)  # Cap at 30 seconds
                log.info(f"Waiting {delay} seconds before retry...")
                time.sleep(delay)
            
            if use_ssl:
                mail = imaplib.IMAP4_SSL(server_host, port)
            else:
                mail = imaplib.IMAP4(server_host, port)
            
            # Set a longer timeout for operations
            mail.socket().settimeout(settings['timeout'])
            
            # Login
            mail.login(email_addr, password)
            
            # Generate a session ID
            session_id = str(uuid.uuid4())
            
            # Store the connection in our dictionary
            email_connections[session_id] = {
                'connection': mail,
                'email': email_addr,
                'server': server_host,
                'created_at': datetime.now(),
                'last_activity': datetime.now(),
                'selected_mailbox': None
            }
            
            log.info(f"Successfully connected to {server_host} for {email_addr}")
            
            return {
                'success': True,
                'message': f'Successfully connected to {server_host}',
                'session_id': session_id
            }
        except imaplib.IMAP4.error as e:
            last_exception = e
            error_msg = str(e).lower()
            
            # Check for specific errors that might be retryable
            if "try again" in error_msg or "temporary" in error_msg:
                log.warning(f"Temporary error connecting to {server_host}, retrying: {str(e)}")
                continue
                
            # Authentication errors should not be retried
            if "login" in error_msg or "authentication" in error_msg or "auth" in error_msg:
                log.error(f"Authentication error for {email_addr} on {server_host}: {str(e)}")
                return {
                    'success': False,
                    'error': f'Login failed: {str(e)}'
                }
                
            # Other IMAP errors, log and return
            log.error(f"IMAP error connecting to {server_host}: {str(e)}")
            return {
                'success': False,
                'error': f'IMAP error: {str(e)}'
            }
        except socket.error as socket_error:
            last_exception = socket_error
            error_code = getattr(socket_error, 'errno', None)
            
            # Connection refused or timeout are worth retrying
            if error_code in (10061, 10060) or "timed out" in str(socket_error).lower():
                log.warning(f"Connection issue with {server_host}, retrying: {str(socket_error)}")
                continue
                
            log.error(f"Socket error connecting to {server_host}: {str(socket_error)}")
            return {
                'success': False,
                'error': f'Connection error: {str(socket_error)}'
            }
        except Exception as e:
            last_exception = e
            log.error(f"Unexpected error connecting to {server_host}: {str(e)}")
            
            # For generic errors, only retry once
            if attempt < 1:
                continue
                
            return {
                'success': False,
                'error': f'Error connecting to email: {str(e)}'
            }
    
    # If we got here, all retries failed
    log.error(f"All connection attempts to {server_host} failed: {str(last_exception)}")
    return {
        'success': False,
        'error': f'Failed to connect after {settings["retry_count"]} attempts: {str(last_exception)}'
    }

def disconnect_email(session_id):
    """Disconnect from email server"""
    if session_id not in email_connections:
        return {
            'success': True,
            'message': 'Session already expired'
        }
    
    try:
        # Get the connection
        mail = email_connections[session_id]['connection']
        
        # Close the connection properly
        try:
            mail.close()
        except:
            pass
            
        try:
            mail.logout()
        except:
            pass
        
        # Remove from connections dictionary
        del email_connections[session_id]
        
        return {
            'success': True,
            'message': 'Successfully disconnected from email server'
        }
    except Exception as e:
        log.error(f"Error disconnecting: {str(e)}")
        
        # Still remove it from the dictionary even if there was an error
        if session_id in email_connections:
            del email_connections[session_id]
            
        return {
            'success': True,
            'message': 'Disconnected from email server with warnings'
        }
