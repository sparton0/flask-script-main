from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context, send_file
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import base64, os, time, queue, shutil
from threading import Event
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.common.exceptions import TimeoutException
import pytz
from datetime import datetime, timedelta
import pathlib
import zipfile
import io

scraping_event = Event()
log_queue = queue.Queue()
driver = None
app = Flask(__name__, static_folder='.')

# Define downloads directory relative to the script location
SAVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
os.makedirs(SAVE_DIR, exist_ok=True)

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

def initialize_driver(download_dir):
    """Initialize the Chrome driver with custom download directory."""
    chrome_options = Options()
    chrome_options.add_argument('--headless')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument("--window-size=1920,1080")
    
    # Set up download preferences with absolute path
    prefs = {
        "download.default_directory": os.path.abspath(download_dir),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True
    }
    chrome_options.add_experimental_option("prefs", prefs)
    
    service = Service()
    driver = webdriver.Chrome(service=service, options=chrome_options)
    
    return driver

@app.route('/scrape', methods=['POST'])
def scrape():
    try:
        global SAVE_DIR, driver
        scraping_event.clear()
        scraping_event.set()
        
        while not log_queue.empty():
            log_queue.get()

        data = request.get_json()
        login_url = data.get("loginUrl")
        table_urls = data.get("urls", [])
        folder_name = data.get("folderName", "").strip()
        custom_save_path = data.get("customPath", "").strip()
        
        # Fix index handling
        try:
            start_index = int(data.get("startIndex", 1))
            if start_index < 1:
                start_index = 1
        except (TypeError, ValueError):
            start_index = 1
        
        try:
            last_index = data.get("lastIndex")
            if last_index is not None:
                last_index = int(last_index)
                if last_index < start_index:
                    last_index = None
        except (TypeError, ValueError):
            last_index = None

        log_message(f"Processing request with start_index: {start_index}, last_index: {last_index}", level='INFO', flush=True)

        if not login_url or not table_urls or not folder_name:
            log_message("Missing required fields", level='ERROR', flush=True)
            return jsonify({"message": "Login URL, table URLs, and folder name are required."}), 400

        # Handle save directory logic
        try:
            if custom_save_path:
                # Clean and normalize the custom path
                custom_save_path = os.path.normpath(custom_save_path)
                
                # Create custom directory if it doesn't exist
                os.makedirs(custom_save_path, exist_ok=True)
                save_dir = custom_save_path
                log_message(f"Using custom save location: {save_dir}", level='INFO', flush=True)
                
                # Also save to downloads directory for web interface
                web_save_dir = SAVE_DIR
                os.makedirs(web_save_dir, exist_ok=True)
            else:
                # Use default downloads directory
                save_dir = SAVE_DIR
                web_save_dir = save_dir
                log_message(f"Using default save location: {save_dir}", level='INFO', flush=True)
        except Exception as e:
            log_message(f"Error setting up save directory: {str(e)}", level='ERROR', flush=True)
            return jsonify({"message": f"Error with save directory: {str(e)}"}), 400

        try:
            driver = initialize_driver(save_dir)
            wait = WebDriverWait(driver, 10)

            driver.get(login_url)
            log_message("Opened login page", level='INFO', flush=True)
            time.sleep(2)

            for table_idx, table_url in enumerate(table_urls):
                log_message(f"Processing table URL {table_idx + 1}: {table_url}", level='INFO', flush=True)
                
                try:
                    driver.execute_script(f"window.open('{table_url}', '_blank');")
                    driver.switch_to.window(driver.window_handles[-1])
                    time.sleep(3)

                    rows = wait.until(EC.presence_of_all_elements_located((By.XPATH, '//tr[starts-with(@id, "R")]')))
                    total_rows = len(rows)
                    log_message(f"Found {total_rows} rows in table {table_idx + 1}", level='INFO', flush=True)

                    # Calculate end index
                    end_index = total_rows
                    if last_index is not None and last_index > 0:
                        end_index = min(last_index, total_rows)
                    
                    # Adjust start_index to be 0-based for array indexing
                    array_start_index = start_index - 1
                    
                    log_message(f"Processing rows from {array_start_index + 1} to {end_index}", level='INFO', flush=True)

                    for index in range(array_start_index, end_index):
                        log_message(f"Processing row {index + 1}", level='INFO', flush=True)
                        rows = driver.find_elements(By.XPATH, '//tr[starts-with(@id, "R")]')
                        
                        # Extract data before clicking
                        try:
                            row_data = rows[index].find_elements(By.TAG_NAME, "td")
                            waqf_id = row_data[0].text.strip() if len(row_data) > 0 else "unknown"
                            property_id = row_data[1].text.strip() if len(row_data) > 1 else "unknown"
                            district = row_data[2].text.strip() if len(row_data) > 2 else "unknown"
                            
                            # Use the requested naming convention
                            filename = f"{waqf_id}_{property_id}_{district}.pdf"
                            filename = "".join(c for c in filename if c.isalnum() or c in ('_', '-', '.'))
                        except Exception as e:
                            log_message(f"Error extracting row data: {str(e)}", level='ERROR', flush=True)
                            filename = f"row{index+1}.pdf"
                        
                        # Click the row
                        driver.execute_script("arguments[0].click();", rows[index])
                        time.sleep(3)
                        
                        # Switch to new tab
                        driver.switch_to.window(driver.window_handles[-1])
                        time.sleep(2)
                        
                        # Check if required text exists in the page
                        try:
                            # Get page source and convert to lowercase for case-insensitive matching
                            page_source = driver.page_source.lower()
                            
                            # Define default variations for "Report Card" text
                            text_variations = [
                                "report card", 
                                "reportcard",
                                "report-card",
                                "property report",
                                "property details"
                            ]
                            
                            log_message("Checking for Report Card text...", level='INFO')
                            
                            # Check if any variation exists in the page
                            found_required_text = False
                            for variation in text_variations:
                                if variation in page_source:
                                    found_required_text = True
                                    log_message(f"Found required text '{variation}' in page", level='INFO')
                                    break
                            
                            # Try to find text using JavaScript as a backup method
                            if not found_required_text:
                                try:
                                    # Build JavaScript check dynamically
                                    js_conditions = " || ".join([
                                        f"document.body.innerText.toLowerCase().includes('{v}')" 
                                        for v in text_variations
                                    ])
                                    js_check = f"return {js_conditions};"
                                    
                                    found_required_text = driver.execute_script(js_check)
                                    if found_required_text:
                                        log_message("Found Report Card text using JavaScript method", level='INFO')
                                except Exception as js_error:
                                    log_message(f"JavaScript text detection error: {str(js_error)}", level='ERROR')
                            
                            # Skip if required text not found
                            if not found_required_text:
                                log_message(f"Report Card text not found, skipping PDF generation for row {index + 1}", level='WARNING')
                                
                                # Close current tab and switch back to table
                                if len(driver.window_handles) > 1:
                                    driver.close()
                                    driver.switch_to.window(driver.window_handles[-1])
                                    time.sleep(1)
                                continue
                        except Exception as e:
                            log_message(f"Error checking for Report Card text: {str(e)}", level='ERROR')
                            # Continue with PDF generation as a fallback
                        
                        # Create and save PDF
                        try:
                            result = driver.execute_cdp_cmd("Page.printToPDF", {"printBackground": True})
                            pdf_data = base64.b64decode(result['data'])
                            
                            # Save to custom location if specified
                            if custom_save_path:
                                custom_pdf_path = os.path.join(custom_save_path, filename)
                                with open(custom_pdf_path, "wb") as f:
                                    f.write(pdf_data)
                                    f.flush()
                                    os.fsync(f.fileno())
                                log_message(f"Saved to custom location: {custom_pdf_path}", level='SUCCESS', flush=True)
                                
                            # Always save to downloads directory
                            web_pdf_path = os.path.join(SAVE_DIR, filename)
                            with open(web_pdf_path, "wb") as f:
                                f.write(pdf_data)
                                f.flush()
                                os.fsync(f.fileno())
                            log_message(f"Saved to downloads: {filename}", level='SUCCESS', flush=True)
                            
                            # Force close the file handles
                            try:
                                f.close()
                            except:
                                pass
                            
                            # Verify file exists and is readable
                            if os.path.exists(web_pdf_path) and os.access(web_pdf_path, os.R_OK):
                                log_message(f"Verified file exists and is readable: {filename}", level='SUCCESS', flush=True)
                            else:
                                log_message(f"File verification failed: {filename}", level='ERROR', flush=True)
                                
                        except Exception as e:
                            log_message(f"Error saving PDF: {str(e)}", level='ERROR', flush=True)
                        
                        try:
                            # Check if scraping was aborted
                            if not scraping_event.is_set():
                                log_message("Operation was aborted, stopping gracefully", level='INFO')
                                # Get current list of files before stopping
                                files = [f for f in os.listdir(SAVE_DIR) if f.endswith('.pdf')]
                                return jsonify(files), 200

                            # Close current tab and switch back to table
                            if len(driver.window_handles) > 1:
                                driver.close()
                                driver.switch_to.window(driver.window_handles[-1])
                                time.sleep(1)
                        except Exception as e:
                            log_message(f"Error in tab management: {str(e)}", level='ERROR', flush=True)
                            # Don't break the loop, try to continue with next item

                except Exception as e:
                    log_message(f"Error processing table {table_idx + 1}: {str(e)}", level='ERROR', flush=True)
                    # Close current tab if open and switch back to main window
                    if len(driver.window_handles) > 1:
                        driver.close()
                        driver.switch_to.window(driver.window_handles[0])
                    continue

                # Close table tab and switch back to main window
                driver.close()
                driver.switch_to.window(driver.window_handles[0])

            # Get list of files for the response
            files = []
            if os.path.exists(SAVE_DIR):
                files = [f for f in os.listdir(SAVE_DIR) if f.endswith('.pdf')]
                log_message(f"Found {len(files)} files at completion", level='INFO')
            
            # Return the list of files
            return jsonify(files), 200

        except Exception as e:
            log_message(f"Error during scraping: {str(e)}", level='ERROR', flush=True)
            return jsonify({"message": f"Error during scraping: {str(e)}"}), 500

        finally:
            if driver:
                try:
                    driver.quit()
                    log_message("Browser closed", level='INFO', flush=True)
                except Exception as e:
                    log_message(f"Error closing browser: {str(e)}", level='ERROR', flush=True)

    except Exception as e:
        log_message(f"Unexpected error: {str(e)}", level='ERROR', flush=True)
        return jsonify({"message": f"Unexpected error: {str(e)}"}), 500

def log_message(message, level='INFO', flush=False):
    # Remove the level prefix for most messages to match the desired format
    if level == 'ERROR':
        formatted_message = f"[ERROR] {message}"
    elif level == 'SUCCESS':
        formatted_message = f" {message}"
    else:
        formatted_message = message
    
    # Print to console for debugging
    print(formatted_message, flush=True)
    
    # Add to queue for streaming
    log_queue.put(formatted_message)
    
    # Force flush the queue if requested
    if flush:
        # This is a workaround to ensure real-time updates
        time.sleep(0.1)

@app.route('/stream')
def stream():
    def generate():
        last_id = 0
        while True:
            if not scraping_event.is_set():
                break
            try:
                # Non-blocking queue get with timeout
                message = log_queue.get(timeout=0.1)
                yield f"data: {message}\n\n"
                last_id += 1
            except queue.Empty:
                # Send a keepalive message every second to maintain the connection
                yield f"data: \n\n"
                time.sleep(0.2)
    return Response(stream_with_context(generate()), mimetype='text/event-stream')

@app.route('/abort', methods=['POST'])
def abort_scraping():
    """Abort the scraping process and return list of saved files"""
    global driver
    try:
        # Clear the scraping event
        scraping_event.clear()
        
        # Close the browser if it's open
        if driver:
            try:
                driver.quit()
            except Exception as e:
                log_message(f"Error closing browser: {str(e)}", level='ERROR')
            finally:
                driver = None
                log_message("Browser closed due to abort request", level='INFO')
        
        # Get list of saved files
        files = []
        try:
            if os.path.exists(SAVE_DIR):
                files = [f for f in os.listdir(SAVE_DIR) if f.endswith('.pdf')]
                log_message(f"Found {len(files)} files after abort", level='INFO')
                for file in files:
                    log_message(f"Available file: {file}", level='INFO')
        except Exception as e:
            log_message(f"Error listing files: {str(e)}", level='ERROR')
        
        # Return just the array of files for consistency
        return jsonify(files)
        
    except Exception as e:
        log_message(f"Error in abort handler: {str(e)}", level='ERROR')
        return jsonify([])

@app.route('/create-zip', methods=['POST'])
def create_zip():
    try:
        data = request.get_json()
        folder_name = data.get('folderName')
        if not folder_name:
            log_message("No folder name provided", level='ERROR')
            return jsonify({"success": False, "message": "Folder name is required"}), 400

        # Create a zip file directly in the SAVE_DIR
        zip_path = os.path.join(SAVE_DIR, f"{folder_name}.zip")
        log_message(f"Creating zip at: {zip_path}", level='INFO')
        
        # Remove existing zip file if it exists
        if os.path.exists(zip_path):
            try:
                os.remove(zip_path)
                log_message("Removed existing zip file", level='INFO')
            except Exception as e:
                log_message(f"Error removing existing zip: {str(e)}", level='ERROR')
                return jsonify({"success": False, "message": f"Error removing existing zip file: {str(e)}"}), 500
            
        # Create the zip file from PDF files in SAVE_DIR
        try:
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for file in os.listdir(SAVE_DIR):
                    if file.endswith('.pdf'):
                        file_path = os.path.join(SAVE_DIR, file)
                        zipf.write(file_path, arcname=file)
                        log_message(f"Added to zip: {file}", level='INFO')
            log_message("ZIP file created successfully", level='SUCCESS')
        except Exception as e:
            log_message(f"Error during zip creation: {str(e)}", level='ERROR')
            return jsonify({"success": False, "message": f"Error creating ZIP file: {str(e)}"}), 500
        
        # Verify the zip was created
        if os.path.exists(zip_path):
            # Return the list of files in the directory
            files = [f for f in os.listdir(SAVE_DIR) if f.endswith('.pdf') or f == f"{folder_name}.zip"]
            return jsonify({"success": True, "files": files, "zip_file": f"{folder_name}.zip"})
        else:
            log_message("ZIP file was not created", level='ERROR')
            return jsonify({"success": False, "message": "Failed to create ZIP file: File not found after creation"}), 500

    except Exception as e:
        log_message(f"Unexpected error creating ZIP file: {str(e)}", level='ERROR')
        return jsonify({"success": False, "message": f"Error creating ZIP file: {str(e)}"}), 500

@app.route('/list_downloads', methods=['GET'])
def list_downloads():
    """List all files in the downloads directory"""
    try:
        # Ensure directory exists
        os.makedirs(SAVE_DIR, exist_ok=True)
        
        # Get list of PDF files
        files = []
        if os.path.exists(SAVE_DIR):
            for filename in os.listdir(SAVE_DIR):
                if filename.endswith('.pdf'):
                    try:
                        file_path = os.path.join(SAVE_DIR, filename)
                        if os.path.exists(file_path) and os.path.isfile(file_path):
                            files.append(filename)
                            log_message(f"Listed file: {filename}", level='INFO')
                    except Exception as e:
                        log_message(f"Error processing file {filename}: {str(e)}", level='ERROR')
        
        log_message(f"Total files found: {len(files)}", level='INFO')
        
        # Return a simple array of filenames for consistency
        return jsonify(files)
        
    except Exception as e:
        log_message(f"Error listing downloads: {str(e)}", level='ERROR')
        return jsonify([])

@app.route('/downloads/<path:filename>')
def download_file(filename):
    """Serve files from the downloads directory"""
    try:
        file_path = os.path.join(SAVE_DIR, filename)
        if not os.path.exists(file_path):
            log_message(f"File not found: {filename}", level='ERROR', flush=True)
            return jsonify({"error": "File not found"}), 404
            
        log_message(f"Serving file: {filename}", level='INFO', flush=True)
        return send_from_directory(SAVE_DIR, filename, as_attachment=True)
    except Exception as e:
        log_message(f"Error serving file {filename}: {str(e)}", level='ERROR', flush=True)
        return jsonify({"error": str(e)}), 500

@app.route('/download_all_zip')
def download_all_zip():
    """Create a ZIP file of all PDFs in the downloads directory and send it to the client"""
    try:
        # Get the folder name from the query parameter
        folder_name = request.args.get('filename', 'downloads')
        
        # Ensure the folder name is safe
        safe_folder_name = "".join(c for c in folder_name if c.isalnum() or c in ('_', '-', '.')).strip()
        if not safe_folder_name:
            safe_folder_name = 'downloads'
        
        # Ensure directory exists
        os.makedirs(SAVE_DIR, exist_ok=True)
            
        # Get all PDF files
        pdf_files = [f for f in os.listdir(SAVE_DIR) if f.endswith('.pdf')]
        log_message(f"Found {len(pdf_files)} files to zip", level='INFO')
        
        if not pdf_files:
            return jsonify({"error": "No PDF files found"}), 404
        
        # Create ZIP file in memory
        memory_file = io.BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for filename in pdf_files:
                file_path = os.path.join(SAVE_DIR, filename)
                if os.path.exists(file_path):
                    zipf.write(file_path, arcname=filename)
                    log_message(f"Added to zip: {filename}", level='INFO')
        
        memory_file.seek(0)
        
        # After creating ZIP, delete all files from the directory
        def delete_files_after_request():
            try:
                # Delete all PDF files
                for filename in os.listdir(SAVE_DIR):
                    if filename.endswith('.pdf') or filename.endswith('.zip'):
                        file_path = os.path.join(SAVE_DIR, filename)
                        try:
                            os.remove(file_path)
                            log_message(f"Deleted file after ZIP download: {filename}", level='INFO')
                        except Exception as e:
                            log_message(f"Error deleting file {filename}: {str(e)}", level='ERROR')
            except Exception as e:
                log_message(f"Error during cleanup: {str(e)}", level='ERROR')
        
        # Create a response with the ZIP file
        response = send_file(
            memory_file,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f'{safe_folder_name}.zip'
        )
        
        # Register a callback to delete files after the response is sent
        response.call_on_close(delete_files_after_request)
        
        log_message(f"ZIP file '{safe_folder_name}.zip' ready for download, files will be deleted after download", level='SUCCESS')
        return response
        
    except Exception as e:
        log_message(f"Error creating zip: {str(e)}", level='ERROR')
        return jsonify({"error": str(e)}), 500

@app.route('/clear_downloads', methods=['POST'])
def clear_downloads():
    """Clear all files from the downloads directory"""
    try:
        deleted_count = 0
        if os.path.exists(SAVE_DIR):
            for filename in os.listdir(SAVE_DIR):
                if filename.endswith('.pdf') or filename.endswith('.zip'):
                    file_path = os.path.join(SAVE_DIR, filename)
                    try:
                        os.remove(file_path)
                        deleted_count += 1
                        log_message(f"Deleted file: {filename}", level='INFO')
                    except Exception as e:
                        log_message(f"Error deleting file {filename}: {str(e)}", level='ERROR')
        
        log_message(f"Cleared {deleted_count} files from downloads directory", level='SUCCESS')
        return jsonify({
            "success": True,
            "message": f"Cleared {deleted_count} files from downloads directory",
            "deleted_count": deleted_count
        })
    except Exception as e:
        log_message(f"Error clearing downloads: {str(e)}", level='ERROR')
        return jsonify({
            "success": False,
            "message": f"Error clearing downloads: {str(e)}"
        }), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
