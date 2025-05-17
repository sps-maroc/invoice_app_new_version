/**
 * Email Import JavaScript
 * Manages the email connection, search, and processing functionality
 */
document.addEventListener('DOMContentLoaded', function() {
    // Global variables
    let currentSessionId = null;
    let currentEmailAccount = null;
    let isSearching = false;
    let searchDelay = 0.5; // Default delay between email fetches in seconds
    let fetchDelay = 1.0; // Default delay for fetching individual emails
    
    // DOM Elements
    const connectBtn = document.getElementById('connect-btn');
    const disconnectBtn = document.getElementById('disconnect-btn');
    const searchEmailsBtn = document.getElementById('search-emails-btn');
    const emailProcessingSection = document.getElementById('email-processing-section');
    const connectionIndicator = document.getElementById('connection-indicator');
    const connectionStatus = document.getElementById('connection-status');
    const emailListContainer = document.getElementById('email-list');
    const emailPreviewContainer = document.getElementById('email-preview');
    const emailSearch = document.getElementById('email-search');
    const refreshEmailsBtn = document.getElementById('refresh-emails-btn');
    const selectAllBtn = document.getElementById('select-all-btn');
    const deselectAllBtn = document.getElementById('deselect-all-btn');
    const processSelectedBtn = document.getElementById('process-selected-btn');
    const selectedCountSpan = document.getElementById('selected-count');
    const emailLoadingSpinner = document.getElementById('email-loading-spinner');
    const emailListPlaceholder = document.getElementById('email-list-placeholder');
    const logContainer = document.getElementById('log-container');
    const togglePasswordBtn = document.querySelector('.toggle-password');
    const processAttachmentsBtn = document.getElementById('process-attachments-btn');
    const savedAccountsSelect = document.getElementById('saved-accounts');
    const deleteAccountBtn = document.getElementById('delete-account-btn');
    const saveAccountCheckbox = document.getElementById('save-account');
    
    // IMAP Server selector
    const imapServerSelect = document.getElementById('imap_server');
    const customServerOptions = document.querySelector('.custom-server-options');
    
    // State
    let isConnected = false;
    let emails = [];
    let selectedEmails = new Set();
    let attachments = [];
    let sessionId = null;
    let savedAccounts = [];
    let currentBatchId = null;
    
    // Toggle password visibility
    if (togglePasswordBtn) {
        togglePasswordBtn.addEventListener('click', function() {
            const passwordInput = document.getElementById('password');
            const icon = this.querySelector('i');
            
            if (passwordInput.type === 'password') {
                passwordInput.type = 'text';
                icon.classList.remove('bi-eye');
                icon.classList.add('bi-eye-slash');
            } else {
                passwordInput.type = 'password';
                icon.classList.remove('bi-eye-slash');
                icon.classList.add('bi-eye');
            }
        });
    }
    
    // Show custom server options when custom is selected
    if (imapServerSelect) {
        imapServerSelect.addEventListener('change', function() {
            if (this.value === 'custom') {
                customServerOptions.classList.remove('d-none');
            } else {
                customServerOptions.classList.add('d-none');
            }
        });
    }
    
    // Load saved accounts
    function loadSavedAccounts() {
        if (!savedAccountsSelect) return;
        
        savedAccountsSelect.innerHTML = '<option value="">-- Select a saved account --</option>';
        
        // Fetch accounts from server
        axios.get('/api/email/accounts')
            .then(response => {
                if (response.data.success) {
                    savedAccounts = response.data.accounts || [];
                    
                    // Populate dropdown
                    savedAccounts.forEach((account, index) => {
                        const option = document.createElement('option');
                        option.value = account.id;
                        option.textContent = account.email;
                        savedAccountsSelect.appendChild(option);
                    });
                    
                    addLogEntry('Loaded saved email accounts', 'info');
                } else {
                    addLogEntry('Failed to load email accounts: ' + (response.data.error || 'Unknown error'), 'error');
                }
            })
            .catch(error => {
                addLogEntry('Error loading email accounts: ' + error.message, 'error');
            });
    }
    
    // Initialize saved accounts
    loadSavedAccounts();
    
    // Select saved account
    if (savedAccountsSelect) {
        savedAccountsSelect.addEventListener('change', function() {
            const selectedId = parseInt(this.value);
            if (!isNaN(selectedId) && selectedId > 0) {
                // Fetch the account details including password
                axios.get(`/api/email/account/${selectedId}`)
                    .then(response => {
                        if (response.data.success) {
                            const account = response.data.account;
                            document.getElementById('email').value = account.email;
                            document.getElementById('password').value = account.password;
                            document.getElementById('imap_server').value = account.imap_server;
                            
                            if (account.is_custom) {
                                document.getElementById('imap_server').value = 'custom';
                                customServerOptions.classList.remove('d-none');
                                document.getElementById('custom_server').value = account.custom_server || '';
                                document.getElementById('port').value = account.port || 993;
                                document.getElementById('use_ssl').checked = account.use_ssl !== false;
                            } else {
                                customServerOptions.classList.add('d-none');
                            }
                        } else {
                            addLogEntry('Failed to load account details: ' + (response.data.error || 'Unknown error'), 'error');
                        }
                    })
                    .catch(error => {
                        addLogEntry('Error loading account details: ' + error.message, 'error');
                    });
            }
        });
    }
    
    // Delete saved account
    if (deleteAccountBtn) {
        deleteAccountBtn.addEventListener('click', function() {
            if (!savedAccountsSelect) return;
            
            const selectedId = parseInt(savedAccountsSelect.value);
            
            if (!isNaN(selectedId) && selectedId > 0) {
                const accountEmail = savedAccounts.find(a => a.id === selectedId)?.email || 'this account';
                
                if (confirm(`Are you sure you want to delete the account '${accountEmail}'?`)) {
                    axios.delete(`/api/email/account/${selectedId}`)
                        .then(response => {
                            if (response.data.success) {
                                addLogEntry(`Account removed from saved accounts`, 'success');
                                // Reload the accounts list
                                loadSavedAccounts();
                            } else {
                                addLogEntry(`Failed to delete account: ${response.data.error || 'Unknown error'}`, 'error');
                            }
                        })
                        .catch(error => {
                            addLogEntry(`Error deleting account: ${error.message}`, 'error');
                        });
                }
            } else {
                addLogEntry('Please select an account to delete', 'warning');
            }
        });
    }
    
    // Connect to email server
    if (connectBtn) {
        connectBtn.addEventListener('click', function() {
            // Gather connection details
            const email = document.getElementById('email').value;
            const password = document.getElementById('password').value;
            let imapServer = imapServerSelect ? imapServerSelect.value : '';
            let port = 993;
            let useSSL = true;
            let saveAccount = saveAccountCheckbox ? saveAccountCheckbox.checked : false;
            let customServer = null;
            let isCustom = false;
            
            if (imapServer === 'custom') {
                customServer = document.getElementById('custom_server').value;
                port = parseInt(document.getElementById('port').value);
                useSSL = document.getElementById('use_ssl').checked;
                isCustom = true;
                
                if (!customServer) {
                    addLogEntry('Please enter a custom server address', 'error');
                    return;
                }
            }
            
            if (!email || !password || !imapServer) {
                addLogEntry('Please fill in all required fields', 'error');
                return;
            }
            
            // Show loading state
            connectBtn.disabled = true;
            connectBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Connecting...';
            
            addLogEntry(`Connecting to ${isCustom ? customServer : imapServer} for ${email}...`, 'info');
            
            // API request to connect to email server
            axios.post('/api/email/connect', {
                email: email,
                password: password,
                imap_server: imapServer,
                port: port,
                use_ssl: useSSL,
                save_account: saveAccount,
                custom_server: customServer
            })
            .then(response => {
                if (response.data.success) {
                    isConnected = true;
                    // Store session ID for future requests
                    sessionId = response.data.session_id;
                    
                    updateConnectionStatus(true);
                    
                    // Update UI for connected state
                    connectBtn.classList.add('d-none');
                    if (disconnectBtn) disconnectBtn.classList.remove('d-none');
                    if (searchEmailsBtn) searchEmailsBtn.classList.remove('d-none');
                    if (emailProcessingSection) emailProcessingSection.classList.remove('d-none');
                    
                    addLogEntry(response.data.message, 'success');
                    if (emailListPlaceholder) {
                        emailListPlaceholder.textContent = 'Click "Search for Invoices" to fetch emails';
                    }
                    
                    // Reload saved accounts if we saved the account
                    if (saveAccount) {
                        loadSavedAccounts();
                    }
                } else {
                    addLogEntry('Failed to connect: ' + (response.data.error || 'Unknown error'), 'error');
                }
            })
            .catch(error => {
                const errorMsg = error.response?.data?.error || error.message;
                addLogEntry('Connection error: ' + errorMsg, 'error');
            })
            .finally(() => {
                // Reset button state
                connectBtn.disabled = false;
                connectBtn.innerHTML = '<i class="bi bi-plug-fill me-2"></i>Connect to Server';
            });
        });
    }
    
    // Update connection status indicator
    function updateConnectionStatus(connected) {
        if (!connectionIndicator || !connectionStatus) return;
        
        if (connected) {
            connectionIndicator.classList.remove('status-disconnected');
            connectionIndicator.classList.add('status-connected');
            connectionStatus.textContent = 'Connected';
        } else {
            connectionIndicator.classList.remove('status-connected');
            connectionIndicator.classList.add('status-disconnected');
            connectionStatus.textContent = 'Disconnected';
        }
    }
    
    // Add log entry
    function addLogEntry(message, level = 'info') {
        if (!logContainer) return;
        
        const logEntry = document.createElement('div');
        logEntry.className = `log-entry log-${level}`;
        
        const timestamp = new Date().toLocaleTimeString();
        logEntry.textContent = `[${timestamp}] ${message}`;
        
        logContainer.appendChild(logEntry);
        logContainer.scrollTop = logContainer.scrollHeight;
    }
    
    // If we already have alert toast functionality, use that
    function showAlert(message, type = 'info') {
        if (typeof showToast === 'function') {
            showToast(message, type);
        } else if (typeof Swal !== 'undefined') {
            Swal.fire({
                title: type.charAt(0).toUpperCase() + type.slice(1),
                text: message,
                icon: type,
                toast: true,
                position: 'top-end',
                showConfirmButton: false,
                timer: 3000
            });
        } else {
            addLogEntry(message, type);
            alert(message);
        }
    }
    
    function setupEventListeners() {
        // Email connection form
        if (emailForm) {
            emailForm.addEventListener('submit', handleEmailConnect);
        }
        
        // Search form
        if (searchForm) {
            searchForm.addEventListener('submit', handleEmailSearch);
        }
        
        // Delay settings
        if (delaySettings) {
            delaySettings.addEventListener('change', updateDelaySettings);
        }
    }
    
    function updateDelaySettings() {
        if (delaySettings) {
            searchDelay = parseFloat(delaySettings.value) || 0.5;
            fetchDelay = searchDelay * 2; // Fetch delay is double the search delay
            addLogEntry(`Updated delay settings: Search=${searchDelay}s, Fetch=${fetchDelay}s`);
        }
    }
    
    function handleEmailConnect(event) {
        event.preventDefault();
        
        const formData = new FormData(emailForm);
        const data = {
            email: formData.get('email'),
            password: formData.get('password'),
            imap_server: formData.get('imap_server'),
            port: formData.get('port'),
            use_ssl: formData.get('use_ssl') === 'on',
            is_custom: formData.get('imap_server') === 'custom',
            custom_server: formData.get('custom_server'),
            save_account: formData.get('save_account') === 'on'
        };
        
        // Show loading state
        updateConnectionStatus(false, 'Connecting...');
        
        fetch('/api/email/connect', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(data)
        })
        .then(response => response.json())
        .then(result => {
            if (result.success) {
                currentSessionId = result.session_id;
                currentEmailAccount = data.email;
                updateConnectionStatus(true, `Connected as ${data.email}`);
                addLogEntry(`Successfully connected to ${data.email}`, 'success');
                
                // Refresh saved accounts list
                loadSavedAccounts();
                
                // Enable search form
                if (searchForm) {
                    searchForm.classList.remove('disabled');
                }
            } else {
                updateConnectionStatus(false, 'Connection failed');
                addLogEntry(`Connection failed: ${result.error}`, 'error');
                showAlert(result.error, 'error');
            }
        })
        .catch(error => {
            updateConnectionStatus(false, 'Connection error');
            addLogEntry(`Connection error: ${error.message}`, 'error');
            showAlert('Error connecting to email server', 'error');
        });
    }
    
    function handleEmailSearch(event) {
        event.preventDefault();
        
        if (!currentSessionId) {
            showAlert('Please connect to an email account first', 'warning');
            return;
        }
        
        if (isSearching) {
            showAlert('Search already in progress', 'warning');
            return;
        }
        
        isSearching = true;
        const criteria = searchCriteria.value;
        
        // Show loading state
        if (searchResults) {
            searchResults.innerHTML = '<div class="loading">Searching emails...</div>';
        }
        
        addLogEntry(`Searching emails with criteria: ${criteria || 'ALL'}`);
        
        fetch('/api/email/search', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                session_id: currentSessionId,
                criteria: criteria,
                delay: searchDelay
            })
        })
        .then(response => response.json())
        .then(result => {
            isSearching = false;
            
            if (result.success) {
                displaySearchResults(result.emails);
                addLogEntry(`Found ${result.emails.length} emails`, 'success');
            } else {
                if (result.reconnect_required) {
                    updateConnectionStatus(false, 'Connection lost');
                    currentSessionId = null;
                    showAlert('Connection lost. Please reconnect.', 'error');
                } else {
                    showAlert(result.error, 'error');
                }
                addLogEntry(`Search failed: ${result.error}`, 'error');
            }
        })
        .catch(error => {
            isSearching = false;
            showAlert('Error searching emails', 'error');
            addLogEntry(`Search error: ${error.message}`, 'error');
        });
    }
    
    function displaySearchResults(emails) {
        if (!searchResults) return;
        
        if (emails.length === 0) {
            searchResults.innerHTML = '<div class="no-results">No emails found</div>';
            return;
        }
        
        const html = emails.map(email => `
            <div class="email-item ${email.is_read ? 'read' : 'unread'} ${email.hasAttachment ? 'has-attachment' : ''}" 
                 data-id="${email.id}" onclick="fetchEmailContent('${email.id}')">
                <div class="email-header">
                    <span class="email-from">${email.from}</span>
                    <span class="email-date">${email.date}</span>
                </div>
                <div class="email-subject">${email.subject}</div>
                ${email.hasAttachment ? '<div class="attachment-indicator">📎</div>' : ''}
            </div>
        `).join('');
        
        searchResults.innerHTML = html;
    }
    
    function fetchEmailContent(emailId) {
        if (!currentSessionId) {
            showAlert('Please connect to an email account first', 'warning');
            return;
        }
        
        // Show loading state
        if (emailContent) {
            emailContent.innerHTML = '<div class="loading">Loading email...</div>';
        }
        if (emailAttachments) {
            emailAttachments.innerHTML = '';
        }
        
        addLogEntry(`Fetching email ${emailId}`);
        
        fetch('/api/email/fetch', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                session_id: currentSessionId,
                email_id: emailId,
                delay: fetchDelay
            })
        })
        .then(response => response.json())
        .then(result => {
            if (result.success) {
                displayEmailContent(result.email);
                if (result.email.attachments && result.email.attachments.length > 0) {
                    displayAttachments(result.email.attachments, emailId);
                }
                addLogEntry(`Email loaded successfully`, 'success');
            } else {
                if (result.reconnect_required) {
                    updateConnectionStatus(false, 'Connection lost');
                    currentSessionId = null;
                    showAlert('Connection lost. Please reconnect.', 'error');
                } else {
                    showAlert(result.error, 'error');
                }
                addLogEntry(`Failed to load email: ${result.error}`, 'error');
            }
        })
        .catch(error => {
            showAlert('Error loading email', 'error');
            addLogEntry(`Error loading email: ${error.message}`, 'error');
        });
    }
    
    function displayEmailContent(email) {
        if (!emailContent) return;
        
        const html = `
            <div class="email-details">
                <div class="email-header">
                    <h3>${email.subject}</h3>
                    <div class="email-meta">
                        <div>From: ${email.from}</div>
                        <div>Date: ${email.date}</div>
                    </div>
                </div>
                <div class="email-body">
                    ${email.html_body || email.body}
                </div>
            </div>
        `;
        
        emailContent.innerHTML = html;
    }
    
    function displayAttachments(attachments, emailId) {
        if (!emailAttachments) return;
        
        const html = attachments.map((attachment, index) => `
            <div class="attachment-item">
                <div class="attachment-info">
                    <span class="attachment-name">${attachment.filename}</span>
                    <span class="attachment-size">${formatFileSize(attachment.size)}</span>
                </div>
                <button onclick="downloadAttachment('${emailId}', '${attachment.id}')" 
                        class="btn btn-primary">
                    Download
                </button>
            </div>
        `).join('');
        
        emailAttachments.innerHTML = `
            <h4>Attachments (${attachments.length})</h4>
            <div class="attachments-list">${html}</div>
        `;
    }
    
    function downloadAttachment(emailId, attachmentId) {
        if (!currentSessionId) {
            showAlert('Please connect to an email account first', 'warning');
            return;
        }
        
        addLogEntry(`Downloading attachment ${attachmentId} from email ${emailId}`);
        
        fetch('/api/email/download-attachment', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                session_id: currentSessionId,
                email_id: emailId,
                attachment_id: attachmentId,
                delay: fetchDelay
            })
        })
        .then(response => response.json())
        .then(result => {
            if (result.success) {
                // Create a temporary link to download the file
                const link = document.createElement('a');
                link.href = result.download_url;
                link.download = result.filename;
                document.body.appendChild(link);
                link.click();
                document.body.removeChild(link);
                
                addLogEntry(`Attachment downloaded successfully`, 'success');
            } else {
                showAlert(result.error, 'error');
                addLogEntry(`Failed to download attachment: ${result.error}`, 'error');
            }
        })
        .catch(error => {
            showAlert('Error downloading attachment', 'error');
            addLogEntry(`Error downloading attachment: ${error.message}`, 'error');
        });
    }
    
    function formatFileSize(bytes) {
        if (bytes === 0) return '0 Bytes';
        const k = 1024;
        const sizes = ['Bytes', 'KB', 'MB', 'GB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
    }
    
    // Make functions available globally
    window.fetchEmailContent = fetchEmailContent;
    window.downloadAttachment = downloadAttachment;
    window.connectSavedAccount = connectSavedAccount;
    window.deleteSavedAccount = deleteSavedAccount;
});
