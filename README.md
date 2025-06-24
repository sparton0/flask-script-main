# Waqf Scraper

A web application for scraping and downloading property details from the WAMSI portal.

## Features

- Scrape property details from WAMSI portal
- Save pages as PDFs
- Check for "Report Card" text before saving
- Download all PDFs as a ZIP file
- Automatic file cleanup after download

## Deployment on Render

### Option 1: Deploy with GitHub

1. Fork this repository to your GitHub account
2. Go to [Render Dashboard](https://dashboard.render.com/)
3. Click "New" and select "Blueprint"
4. Connect your GitHub account and select the forked repository
5. Render will automatically detect the `render.yaml` file and set up the service
6. Click "Apply" to deploy the application

### Option 2: Deploy with Docker Hub

1. Go to [Render Dashboard](https://dashboard.render.com/)
2. Click "New" and select "Web Service"
3. Select "Docker" as the environment
4. Enter the Docker image URL: `518990/waqf-scraper:latest`
5. Choose a name for your service
6. Set the following:
   - Region: Choose the closest to your location
   - Branch: main
   - Plan: Free
   - Environment Variables: Add `PORT=5000`
7. Click "Create Web Service" to deploy

### Accessing the Application

Once deployed, your application will be available at:
`https://your-service-name.onrender.com`

## Local Development

### Running with Docker

```bash
# Build the Docker image
docker build -t waqf-scraper .

# Run the container
docker run -p 5000:5000 waqf-scraper
```

### Running without Docker

```bash
# Install dependencies
pip install -r requirements.txt

# Run the application
python app.py
```

Then open your browser to `http://localhost:5000` 