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
SAVE_DIR = os.path.abspath("pdf_output")  # Default directory
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
        "download.default_directory": download_dir,
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
        global driver
        scraping_event.clear()
        scraping_event.set()
        
        while not log_queue.empty():
            log_queue.get()

        data = request.get_json()
        log_message(f"Received data: {data}", level='INFO', flush=True)
        
        login_url = data.get("loginUrl")
        table_urls = data.get("urls", [])
        folder_name = data.get("folderName", "").strip()
        
        try:
            start_index = int(data.get("startIndex")) if data.get("startIndex") else 1
            if start_index < 1:
                start_index = 1
        except (TypeError, ValueError):
            start_index = 1
            
        try:
            last_index = int(data.get("lastIndex")) if data.get("lastIndex") else None
            if last_index and last_index < start_index:
                last_index = None
        except (TypeError, ValueError):
            last_index = None

        log_message(f"Processing request with start_index: {start_index}, last_index: {last_index}", level='INFO', flush=True)

        if not login_url or not table_urls or not folder_name:
            log_message("Missing required fields", level='ERROR', flush=True)
            return jsonify({"message": "Login URL, table URLs, and folder name are required."}), 400

        # Use the download location directly in the container
        save_dir = "/app/downloads"
        log_message(f"Using container directory: {save_dir}", level='INFO', flush=True)

        try:
            # Create directory if it doesn't exist
            os.makedirs(save_dir, exist_ok=True)
            # Ensure the directory has proper permissions
            os.chmod(save_dir, 0o777)
            
            log_message(f"Created/verified directory with write permissions: {save_dir}", level='INFO', flush=True)
        except Exception as e:
            log_message(f"Error with directory: {str(e)}", level='ERROR', flush=True)
            return jsonify({"message": f"Error with download directory: {str(e)}"}), 400

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
                    log_message(f"Found {len(rows)} rows in table {table_idx + 1}", level='INFO', flush=True)

                    # Calculate end index
                    end_index = min(last_index, len(rows)) if last_index else len(rows)
                    
                    # Adjust start_index to be 0-based for array indexing
                    array_start_index = start_index - 1
                    
                    for index in range(array_start_index, end_index):
                        log_message(f"Processing row {index + 1}", level='INFO', flush=True)
                        rows = driver.find_elements(By.XPATH, '//tr[starts-with(@id, "R")]')
                        
                        # Click on the row to open the detail page
                        driver.execute_script("arguments[0].click();", rows[index])
                        log_message(f"Clicked on row {index + 1}, waiting for page to load...", level='INFO', flush=True)
                        time.sleep(3)  # Wait for page to load
                        
                        # Validate that we're on a proper detail page by checking for Waqf ID field
                        try:
                            # Check for "Waqf ID" field which seems to be consistently available
                            waqf_id_xpath = "//font[contains(text(), 'Waqf ID')]"
                            
                            try:
                                waqf_id_element = driver.find_element(By.XPATH, waqf_id_xpath)
                                if waqf_id_element:
                                    log_message(f"Valid detail page confirmed: Found 'Waqf ID' field", level='SUCCESS', flush=True)
                                    
                                    # Extract data for filename
                                    try:
                                        # Get data from the original row for the filename
                                        row_data = rows[index].find_elements(By.TAG_NAME, "td")
                                        waqf_id = row_data[0].text.strip() if len(row_data) > 0 else "unknown"
                                        property_id = row_data[1].text.strip() if len(row_data) > 1 else "unknown"
                                        district = row_data[2].text.strip() if len(row_data) > 2 else "unknown"
                                        
                                        # Create filename with extracted data and folder name prefix
                                        filename = f"{index+1}_{waqf_id}_{property_id}.pdf"
                                        # Clean filename to remove any invalid characters
                                        filename = "".join(c for c in filename if c.isalnum() or c in ('_', '-', '.'))
                                    except Exception as e:
                                        log_message(f"Error extracting row data: {str(e)}", level='ERROR', flush=True)
                                        filename = f"{index+1}_row.pdf"  # Fallback to simple naming
                                    
                                    # Create PDF directly using Chrome's print to PDF
                                    try:
                                        result = driver.execute_cdp_cmd("Page.printToPDF", {"printBackground": True})
                                        pdf_data = base64.b64decode(result['data'])
                                        
                                        # Save to the container directory
                                        pdf_path = os.path.join(save_dir, filename)
                                        with open(pdf_path, "wb") as f:
                                            f.write(pdf_data)
                                        log_message(f"Saved: {filename}", level='SUCCESS', flush=True)
                                    except Exception as e:
                                        log_message(f"Error saving PDF: {str(e)}", level='ERROR', flush=True)
                                else:
                                    log_message(f"Invalid page detected for row {index + 1}. 'Waqf ID' field found but empty.", level='ERROR', flush=True)
                            except Exception as e:
                                log_message(f"Invalid page detected for row {index + 1}. 'Waqf ID' field not found: {str(e)}", level='ERROR', flush=True)
                        except Exception as e:
                            log_message(f"Error validating page for row {index + 1}: {str(e)}", level='ERROR', flush=True)
                        
                        # Go back to the table page to continue with the next row
                        try:
                            driver.back()
                            # Wait for the table to load again
                            WebDriverWait(driver, 10).until(EC.presence_of_all_elements_located((By.XPATH, '//tr[starts-with(@id, "R")]')))
                            time.sleep(2)  # Additional wait to ensure page is fully loaded
                        except Exception as e:
                            log_message(f"Error navigating back to table: {str(e)}", level='ERROR', flush=True)
                            break  # Break the loop if we can't navigate back

                except Exception as e:
                    log_message(f"Error processing table {table_idx + 1}: {str(e)}", level='ERROR', flush=True)
                    continue

            return jsonify({"message": f"Scraping completed. PDFs saved and ready for download.", "folderName": folder_name}), 200

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
    global driver
    scraping_event.clear()
    if driver:
        try:
            driver.quit()
            driver = None
            log_message("Browser closed due to abort request", level='INFO', flush=True)
        except Exception as e:
            log_message(f"Error closing browser: {str(e)}", level='ERROR', flush=True)
    return jsonify({"message": "Operation aborted"}), 200

@app.route('/create-zip', methods=['POST'])
def create_zip():
    try:
        data = request.get_json()
        folder_name = data.get('folderName')
        if not folder_name:
            log_message("No folder name provided", level='ERROR', flush=True)
            return jsonify({"message": "Folder name is required"}), 400

        # Path to the folder we want to zip
        folder_path = os.path.join(SAVE_DIR, folder_name)
        log_message(f"Checking folder path: {folder_path}", level='INFO', flush=True)
        
        if not os.path.exists(folder_path):
            log_message(f"Folder not found: {folder_path}", level='ERROR', flush=True)
            return jsonify({"message": "Folder not found"}), 404

        if not os.path.isdir(folder_path):
            log_message(f"Path exists but is not a directory: {folder_path}", level='ERROR', flush=True)
            return jsonify({"message": "Invalid folder path"}), 400

        # List contents of the folder
        files = os.listdir(folder_path)
        log_message(f"Found {len(files)} files in the folder", level='INFO', flush=True)

        # Create a zip file in the same directory
        zip_path = os.path.join(SAVE_DIR, f"{folder_name}.zip")
        log_message(f"Creating zip at: {zip_path}", level='INFO', flush=True)
        
        # Remove existing zip file if it exists
        if os.path.exists(zip_path):
            try:
                os.remove(zip_path)
                log_message("Removed existing zip file", level='INFO', flush=True)
            except Exception as e:
                log_message(f"Error removing existing zip: {str(e)}", level='ERROR', flush=True)
                return jsonify({"message": f"Error removing existing zip file: {str(e)}"}), 500
            
        # Create the zip file
        try:
            shutil.make_archive(os.path.join(SAVE_DIR, folder_name), 'zip', folder_path)
            log_message("ZIP file created successfully", level='SUCCESS', flush=True)
        except Exception as e:
            log_message(f"Error during zip creation: {str(e)}", level='ERROR', flush=True)
            return jsonify({"message": f"Error creating ZIP file: {str(e)}"}), 500
        
        # Verify the zip was created
        if os.path.exists(zip_path):
            return jsonify({
                "message": f"ZIP file created successfully at pdf_output/{folder_name}.zip"
            })
        else:
            log_message("ZIP file was not created", level='ERROR', flush=True)
            return jsonify({"message": "Failed to create ZIP file: File not found after creation"}), 500

    except Exception as e:
        log_message(f"Unexpected error creating ZIP file: {str(e)}", level='ERROR', flush=True)
        return jsonify({"message": f"Error creating ZIP file: {str(e)}"}), 500

@app.route('/downloads/<path:filename>')
def download_file(filename):
    """Serve files from the downloads directory"""
    return send_from_directory('/app/downloads', filename)

@app.route('/list_downloads', methods=['GET'])
def list_downloads():
    """List all files in the downloads directory"""
    try:
        files = os.listdir('/app/downloads')
        pdf_files = [f for f in files if f.endswith('.pdf')]
        return jsonify({"files": pdf_files}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/download_all_zip')
def download_all_zip():
    """Create a ZIP file of all PDFs in the downloads directory and send it to the client"""
    try:
        # Path to the downloads directory
        downloads_dir = '/app/downloads'
        
        # Get the filename from the query parameter or use a default
        filename = request.args.get('filename', 'downloads')
        
        # Ensure the filename is safe and has a .zip extension
        safe_filename = "".join(c for c in filename if c.isalnum() or c in ('_', '-', '.')).strip()
        if not safe_filename:
            safe_filename = 'downloads'
        if not safe_filename.endswith('.zip'):
            safe_filename += '.zip'
        
        # Check if the directory exists
        if not os.path.exists(downloads_dir):
            return jsonify({"error": "Downloads directory not found"}), 404
        
        # List all PDF files
        files = [f for f in os.listdir(downloads_dir) if f.endswith('.pdf')]
        
        if not files:
            return jsonify({"error": "No PDF files found"}), 404
        
        # Create a BytesIO object to store the ZIP file
        memory_file = io.BytesIO()
        
        # Create the ZIP file in memory
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for file in files:
                file_path = os.path.join(downloads_dir, file)
                zipf.write(file_path, arcname=file)
        
        # Seek to the beginning of the BytesIO object
        memory_file.seek(0)
        
        # Send the ZIP file to the client
        return send_file(
            memory_file,
            mimetype='application/zip',
            as_attachment=True,
            download_name=safe_filename
        )
    
    except Exception as e:
        log_message(f"Error creating ZIP file: {str(e)}", level='ERROR', flush=True)
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
