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

scraping_event = Event()
log_queue = queue.Queue()
driver = None
app = Flask(__name__)
SAVE_DIR = os.path.abspath("pdf_output")  # Default directory
os.makedirs(SAVE_DIR, exist_ok=True)

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

def get_chrome_options(save_location):
    options = Options()
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    
    # Set download preferences
    prefs = {
        "download.default_directory": save_location,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True
    }
    options.add_experimental_option("prefs", prefs)
    return options

def initialize_driver(save_dir):
    try:
        # Get ChromeDriver path using the default installation
        driver_path = ChromeDriverManager().install()
        
        # Ensure we're using the correct chromedriver.exe file
        driver_dir = os.path.dirname(driver_path)
        if not driver_path.endswith('chromedriver.exe'):
            # Look for chromedriver.exe in the directory
            possible_paths = [
                os.path.join(driver_dir, 'chromedriver.exe'),
                os.path.join(driver_dir, 'chromedriver-win32', 'chromedriver.exe'),
                os.path.join(driver_dir, 'chromedriver-win64', 'chromedriver.exe')
            ]
            
            for path in possible_paths:
                if os.path.exists(path):
                    driver_path = path
                    break
            else:
                raise Exception(f"Could not find chromedriver.exe in {driver_dir}")
        
        log_message(f"Using ChromeDriver at: {driver_path}", level='INFO')
        
        # Create service with explicit path
        service = Service(executable_path=driver_path)
        
        # Initialize Chrome with options
        options = get_chrome_options(save_dir)
        driver = webdriver.Chrome(service=service, options=options)
        return driver
    except Exception as e:
        log_message(f"Error initializing ChromeDriver: {str(e)}", level='ERROR')
        if "not a valid Win32 application" in str(e):
            log_message("This error typically occurs when ChromeDriver is not compatible with your system.", level='WARNING')
            log_message("Please ensure you have the latest version of Chrome browser installed.", level='WARNING')
            log_message("You may need to manually download ChromeDriver from: https://chromedriver.chromium.org/downloads", level='WARNING')
            log_message("After downloading, extract chromedriver.exe and place it in: " + driver_dir, level='WARNING')
        raise

@app.route('/scrape', methods=['POST'])
def scrape():
    # Maintenance window: 10:58pm to 12:31am IST
    ist = pytz.timezone('Asia/Kolkata')
    now_ist = datetime.now(ist)
    
    # Set maintenance start time (10:58 PM)
    start_maint = now_ist.replace(hour=22, minute=58, second=0, microsecond=0)
    
    # Set maintenance end time (12:31 AM next day)
    end_maint = now_ist.replace(hour=0, minute=31, second=0, microsecond=0)
    if now_ist.hour >= 22:  # If current time is after 10 PM
        end_maint = end_maint + timedelta(days=1)
    
    # Check if current time is within maintenance window
    in_maintenance = False
    if now_ist.hour >= 22:  # After 10 PM
        in_maintenance = now_ist >= start_maint
    elif now_ist.hour < 1:  # Before 1 AM
        in_maintenance = now_ist < end_maint
    
    if in_maintenance:
        return jsonify({"message": "Website under maintenance. Please try again after 12:31 AM IST."}), 503

    global SAVE_DIR, driver
    scraping_event.clear()
    scraping_event.set()
    
    while not log_queue.empty():
        log_queue.get()

    data = request.get_json()
    login_url = data.get("loginUrl")
    table_urls = data.get("urls", [])
    folder_name = data.get("folderName")
    start_index = data.get("startIndex")
    last_index = data.get("lastIndex")
    try:
        start_index = int(start_index)
        if start_index < 1:
            start_index = 0
        else:
            start_index -= 1  # Convert to zero-based index
    except (TypeError, ValueError):
        start_index = 0

    # Parse last_index (convert to int, None if not provided or invalid)
    try:
        last_index = int(last_index)
        if last_index < 1:
            last_index = None
        else:
            last_index = last_index  # 1-based, will use as inclusive
    except (TypeError, ValueError):
        last_index = None

    if not login_url or not table_urls or not folder_name:
        return jsonify({"message": "Login URL, table URLs, and folder name are required."}), 400

    # Create the user-specified folder inside pdf_output
    base_dir = os.path.abspath("pdf_output")
    save_dir = os.path.join(base_dir, folder_name)
    try:
        os.makedirs(save_dir, exist_ok=True)
        log_message(f"PDFs will be saved to: {save_dir}", level='INFO')
    except Exception as e:
        return jsonify({"message": f"Error creating save directory: {str(e)}"}), 400

    driver = None
    try:
        driver = initialize_driver(save_dir)
        wait = WebDriverWait(driver, 10)

        driver.get(login_url)
        log_message("Opened login page", level='INFO')
        time.sleep(2)

        for table_idx, table_url in enumerate(table_urls):
            log_message(f"Opening table URL {table_idx + 1}", level='INFO')
            driver.execute_script(f"window.open('{table_url}', '_blank');")
            driver.switch_to.window(driver.window_handles[-1])
            time.sleep(3)

            try:
                rows = wait.until(EC.presence_of_all_elements_located((By.XPATH, '//tr[starts-with(@id, "R")]')))
                log_message(f"Found {len(rows)} rows in table {table_idx + 1}", level='INFO')
            except TimeoutException:
                log_message(f"Table {table_idx + 1} took too long to load or has too much data. Skipping this table.", level='WARNING')
                driver.close()
                driver.switch_to.window(driver.window_handles[0])
                continue

            # Calculate the end index for the loop
            end_index = len(rows)
            if last_index is not None:
                end_index = min(last_index, len(rows))

            for index in range(start_index, end_index):
                log_message(f"Processing row {index + 1}", level='INFO')
                rows = driver.find_elements(By.XPATH, '//tr[starts-with(@id, "R")]')
                driver.execute_script("arguments[0].click();", rows[index])
                time.sleep(3)

                # Check for error/placeholder content in the page
                page_source = driver.page_source.lower()
                error_keywords = [
                    'no data', 'session expired', 'error', 'maintenance', 'not available', 'temporarily unavailable', 'try again later', 'invalid', 'unauthorized', 'forbidden',
                    'user validation required to continue'
                ]
                if any(keyword in page_source for keyword in error_keywords):
                    log_message(f"Error: Unexpected content detected on row {index + 1}. Stopping automation.", level='ERROR')
                    return jsonify({"message": "Error: Unexpected content detected (e.g., session expired, no data, or maintenance page). Automation stopped."}), 500

                # Extract data from the table row
                try:
                    # Assuming the data is in specific columns, adjust the indices as needed
                    row_data = rows[index].find_elements(By.TAG_NAME, "td")
                    waqf_id = row_data[0].text.strip() if len(row_data) > 0 else "unknown"
                    property_id = row_data[1].text.strip() if len(row_data) > 1 else "unknown"
                    district = row_data[2].text.strip() if len(row_data) > 2 else "unknown"
                    # state = row_data[3].text.strip() if len(row_data) > 3 else "unknown"
                    
                    # Create filename with extracted data
                    filename = f"{waqf_id}_{property_id}_{district}.pdf"
                    # Clean filename to remove any invalid characters
                    filename = "".join(c for c in filename if c.isalnum() or c in ('_', '-', '.'))
                except Exception as e:
                    log_message(f"Error extracting row data: {str(e)}", level='ERROR')
                    filename = f"table{table_idx+1}_row{index+1}.pdf"  # Fallback to original naming

                driver.switch_to.window(driver.window_handles[-1])
                time.sleep(2)

                result = driver.execute_cdp_cmd("Page.printToPDF", {"printBackground": True})
                pdf_data = base64.b64decode(result['data'])

                with open(os.path.join(save_dir, filename), "wb") as f:
                    f.write(pdf_data)
                log_message(f" Saved: {filename}", level='SUCCESS')

                driver.close()
                driver.switch_to.window(driver.window_handles[-1])
                time.sleep(1)

            driver.close()
            driver.switch_to.window(driver.window_handles[0])

        return jsonify({"message": f" Scraping completed. PDFs saved in 'pdf_output/{folder_name}' folder."}, level='SUCCESS')

    except Exception as e:
        log_message("Error: " + str(e), level='ERROR')
        return jsonify({"message": f" Error: {str(e)}"}), 500

    finally:
        if driver is not None:
            try:
                driver.quit()
                log_message("Browser closed", level='INFO')
            except Exception as e:
                log_message(f"Error closing browser: {str(e)}", level='ERROR')

def log_message(message, level='INFO'):
    # Remove the level prefix for most messages to match the desired format
    if level in ['INFO', 'SUCCESS']:
        log_queue.put(message)
        print(message)
    else:
        # Keep the prefix only for errors and warnings
        prefix = {
            'ERROR': '[ERROR] ',
            'WARNING': '[WARNING] '
        }.get(level, '')
        log_queue.put(prefix + message)
        print(prefix + message)

@app.route('/stream')
def stream():
    def generate():
        while True:
            try:
                message = log_queue.get(timeout=1)
                yield f"data: {message}\n\n"
            except queue.Empty:
                if not scraping_event.is_set():
                    break
    return Response(stream_with_context(generate()), mimetype='text/event-stream')

@app.route('/abort', methods=['POST'])
def abort_scraping():
    global driver
    scraping_event.clear()
    if driver:
        try:
            driver.quit()
            driver = None
            log_message("Browser closed due to abort request", level='INFO')
        except Exception as e:
            log_message(f"Error closing browser: {str(e)}", level='ERROR')
    return jsonify({"message": "Operation aborted"}), 200

@app.route('/create-zip', methods=['POST'])
def create_zip():
    try:
        data = request.get_json()
        folder_name = data.get('folderName')
        if not folder_name:
            log_message("No folder name provided", level='ERROR')
            return jsonify({"message": "Folder name is required"}), 400

        # Path to the folder we want to zip
        folder_path = os.path.join(SAVE_DIR, folder_name)
        log_message(f"Checking folder path: {folder_path}", level='INFO')
        
        if not os.path.exists(folder_path):
            log_message(f"Folder not found: {folder_path}", level='ERROR')
            return jsonify({"message": "Folder not found"}), 404

        if not os.path.isdir(folder_path):
            log_message(f"Path exists but is not a directory: {folder_path}", level='ERROR')
            return jsonify({"message": "Invalid folder path"}), 400

        # List contents of the folder
        files = os.listdir(folder_path)
        log_message(f"Found {len(files)} files in the folder", level='INFO')

        # Create a zip file in the same directory
        zip_path = os.path.join(SAVE_DIR, f"{folder_name}.zip")
        log_message(f"Creating zip at: {zip_path}", level='INFO')
        
        # Remove existing zip file if it exists
        if os.path.exists(zip_path):
            try:
                os.remove(zip_path)
                log_message("Removed existing zip file", level='INFO')
            except Exception as e:
                log_message(f"Error removing existing zip: {str(e)}", level='ERROR')
                return jsonify({"message": f"Error removing existing zip file: {str(e)}"}), 500
            
        # Create the zip file
        try:
            shutil.make_archive(os.path.join(SAVE_DIR, folder_name), 'zip', folder_path)
            log_message("ZIP file created successfully", level='SUCCESS')
        except Exception as e:
            log_message(f"Error during zip creation: {str(e)}", level='ERROR')
            return jsonify({"message": f"Error creating ZIP file: {str(e)}"}), 500
        
        # Verify the zip was created
        if os.path.exists(zip_path):
            return jsonify({
                "message": f"ZIP file created successfully at pdf_output/{folder_name}.zip"
            })
        else:
            log_message("ZIP file was not created", level='ERROR')
            return jsonify({"message": "Failed to create ZIP file: File not found after creation"}), 500

    except Exception as e:
        log_message(f"Unexpected error creating ZIP file: {str(e)}", level='ERROR')
        return jsonify({"message": f"Error creating ZIP file: {str(e)}"}), 500

if __name__ == '__main__':
    app.run(debug=True)
