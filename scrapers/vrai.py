import asyncio
import re
import os
import uuid
import logging
import base64
import random
import time
from datetime import datetime
from io import BytesIO
import httpx
from PIL import Image as PILImage
from openpyxl import Workbook
from openpyxl.drawing.image import Image as ExcelImage
from flask import Flask
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError, Error
from utils import get_public_ip, log_event, sanitize_filename
from database import insert_into_db
from limit_checker import update_product_count
import json
import mimetypes
from proxysetup import get_browser_with_proxy_strategy
# Load environment
load_dotenv()
PROXY_URL = os.getenv("PROXY_URL")

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
EXCEL_DATA_PATH = os.path.join(BASE_DIR, 'static', 'ExcelData')
IMAGE_SAVE_PATH = os.path.join(BASE_DIR, 'static', 'Images')

# Resize image if needed
def resize_image(image_data, max_size=(100, 100)):
    try:
        img = PILImage.open(BytesIO(image_data))
        img.thumbnail(max_size, PILImage.LANCZOS)
        buffer = BytesIO()
        img.save(buffer, format="JPEG", quality=85)
        return buffer.getvalue()
    except Exception as e:
        log_event(f"Error resizing image: {e}")
        return image_data


# Async image downloader
async def download_image_async(image_url, product_name, timestamp, image_folder, unique_id, retries=3):
    if not image_url or image_url == "N/A":
        return "N/A"

    image_filename = f"{unique_id}_{timestamp}.png"
    image_full_path = os.path.join(image_folder, image_filename)

    async with httpx.AsyncClient(timeout=10.0) as client:
        for attempt in range(retries):
            try:
                response = await client.get(image_url)
                response.raise_for_status()
                with open(image_full_path, "wb") as f:
                    f.write(response.content)
                return image_full_path
            except httpx.RequestError as e:
                logging.warning(f"Retry {attempt + 1}/{retries} - Error downloading {product_name}: {e}")
    logging.error(f"Failed to download {product_name} after {retries} attempts.")
    return "N/A"

# Human-like delay
def random_delay(min_sec=1, max_sec=3):
    time.sleep(random.uniform(min_sec, max_sec))

# Scroll to bottom of page to load all products
async def scroll_to_bottom(page):
    last_height = await page.evaluate("document.body.scrollHeight")
    while True:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(random.uniform(1, 3))  # Random delay between scrolls
        
        # Check if we've reached the bottom
        new_height = await page.evaluate("document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height

# Reliable page.goto wrapper
async def safe_goto_and_wait(page, url, retries=3):
    for attempt in range(retries):
        try:
            print(f"[Attempt {attempt + 1}] Navigating to: {url}")
            await page.goto(url, timeout=180_000, wait_until="domcontentloaded")
            await page.wait_for_selector(".small-product-grid", state="attached", timeout=30000)
            print("[Success] Product listing loaded.")
            return
        except (Error, TimeoutError) as e:
            logging.warning(f"Attempt {attempt + 1} failed for {url}: {e}")
            if attempt < retries - 1:
                random_delay(1, 3)
            else:
                raise

# Main scraper function
async def handle_vrai(url, max_pages=None):
    ip_address = get_public_ip()
    logging.info(f"Scraping started for: {url} from IP: {ip_address}")

    os.makedirs(EXCEL_DATA_PATH, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    image_folder = os.path.join(IMAGE_SAVE_PATH, timestamp)
    os.makedirs(image_folder, exist_ok=True)

    wb = Workbook()
    sheet = wb.active
    sheet.title = "Products"
    headers = ["Current Date", "Header", "Product Name", "Image", "Kt", "Price", "Total Dia wt", "Time", "ImagePath", "Additional Info"]
    sheet.append(headers)

    all_records = []
    filename = f"handle_vrai_{datetime.now().strftime('%Y-%m-%d_%H.%M')}.xlsx"
    file_path = os.path.join(EXCEL_DATA_PATH, filename)
    prev_prod_count = 0
    load_more_clicks = 1
    
    while load_more_clicks <= max_pages:
        browser = None
        page = None
        try:
            async with async_playwright() as p:
                browser, page = await get_browser_with_proxy_strategy(p, url, ".small-product-grid")
                log_event(f"Successfully loaded: {url}")

                try:
                    # Handle overlay if present
                    overlay_selector = "div.relative.z-\\[100\\]"
                    close_button_selector = "button.absolute.right-sm.top-sm.z-10"
                    
                    if await page.is_visible(overlay_selector):
                        log_event("Overlay detected, attempting to close...")
                        try:
                            await page.click(close_button_selector)
                            log_event("Overlay closed successfully")
                        except Exception as e:
                            log_event(f"Error clicking close button: {e}")
                            await page.evaluate('''() => {
                                const overlay = document.querySelector('div.relative.z-\\[100\\]');
                                if (overlay) overlay.remove();
                            }''')
                            log_event("Overlay removed via JavaScript")
                except Exception as e:
                    log_event(f"Error handling overlay: {e}. Continuing anyway...")

                # Scroll to load all items
                await scroll_to_bottom(page)
                
                page_title = await page.title()
                current_date = datetime.now().strftime("%Y-%m-%d")
                time_only = datetime.now().strftime("%H.%M")

                # Get all product tiles
                product_wrapper = await page.query_selector("div.small-product-grid")
                products = await page.query_selector_all("div.plp-product-item") if product_wrapper else []
                max_prod = len(products)
                products = products[prev_prod_count: min(max_prod, prev_prod_count + 30)]
                prev_prod_count += len(products)

                if len(products) == 0:
                    log_event("No new products found, stopping the scraper.")
                    break

                logging.info(f"New products found: {len(products)}")
                print(f"New products found: {len(products)}")
                records = []
                image_tasks = []
                
                for row_num, product in enumerate(products, start=len(sheet["A"]) + 1):
                    additional_info = []
                    try:
                        # Extract product name
                        name_tag = await product.query_selector(".title-m")
                        product_name = (await name_tag.inner_text()).strip() if name_tag else "N/A"
                    except Exception:
                        product_name = "N/A"

                    # Price handling
                    price = "N/A"
                    try:
                        # Extract price element
                        price_tag = await product.query_selector(".body-m")
                        if price_tag:
                            price_text = (await price_tag.inner_text()).strip()
                            # Clean up price string
                            price_text = re.sub(r'\s+', ' ', price_text).strip()
                            
                            # Check if it's a "From" price
                            if price_text.startswith("From"):
                                price = price_text.replace("From", "").strip()
                                additional_info.append("Multiple price points available")
                            else:
                                price = price_text
                    except Exception:
                        price = "N/A"

                    # Extract product description and style info
                    description = product_name
                    try:
                        # Get style information (like "Cushion · Yellow Gold")
                        style_tag = await product.query_selector(".body-m:has(span.whitespace-nowrap)")
                        if style_tag:
                            style_info = (await style_tag.inner_text()).strip()
                            description = f"{product_name} - {style_info}"
                            additional_info.append(f"Style: {style_info}")
                    except Exception:
                        pass

                    # Extract all available images
                    image_urls = []
                    try:
                        # Get all image slides
                        img_slides = await product.query_selector_all(".embla__slide img")
                        for img in img_slides:
                            img_url = await img.get_attribute("src")
                            if img_url:
                                # Clean up the URL by removing query parameters
                                img_url = img_url.split('?')[0]
                                image_urls.append(img_url)
                                
                        # Use first available image as primary image_url
                        image_url = image_urls[0] if image_urls else "N/A"
                        
                        # Add info about multiple images if available
                        if len(image_urls) > 1:
                            additional_info.append(f"Multiple images available ({len(image_urls)})")
                    except Exception as e:
                        log_event(f"Error getting image URL: {e}")
                        image_url = "N/A"

                    # Extract gold type (kt) from product name/description
                    gold_type_pattern = r"\b\d{1,2}(?:K|kt|ct|Kt)\b|\bPlatinum\b|\bSilver\b|\bWhite Gold\b|\bYellow Gold\b|\bRose Gold\b"
                    gold_type_match = re.search(gold_type_pattern, description, re.IGNORECASE)
                    kt = gold_type_match.group() if gold_type_match else "Not found"

                    # Extract diamond weight from description
                    diamond_weight_pattern = r"\b\d+(\.\d+)?\s*(?:ct|tcw|carat)\b"
                    diamond_weight_match = re.search(diamond_weight_pattern, description, re.IGNORECASE)
                    diamond_weight = diamond_weight_match.group() if diamond_weight_match else "N/A"

                    # Collect additional product information
                    try:
                        # Check for customization option
                        customize_btn = await product.query_selector("div.absolute.left-0 img[alt*='Customize']")
                        if customize_btn:
                            additional_info.append("Customizable")
                            
                        # Check for product ID in the div
                        product_id = await product.get_attribute("id")
                        if product_id and "plp-product-item" in product_id:
                            clean_id = product_id.replace("plp-product-item-", "")
                            additional_info.append(f"Product ID: {clean_id}")
                            
                        # Check for alt text on images
                        alt_texts = set()
                        img_tags = await product.query_selector_all("img")
                        for img in img_tags:
                            alt = await img.get_attribute("alt")
                            if alt and alt.lower() not in ["alt", "product image", "icon: customize"]:
                                alt_texts.add(alt.strip())
                        if alt_texts:
                            additional_info.append(f"Image alts: {' | '.join(alt_texts)}")
                            
                    except Exception as e:
                        log_event(f"Error collecting additional info: {e}")

                    # Combine all additional info with pipe delimiter
                    additional_info_str = " | ".join(additional_info) if additional_info else ""

                    unique_id = str(uuid.uuid4())
                    if image_url and image_url != "N/A":
                        image_tasks.append((row_num, unique_id, asyncio.create_task(
                            download_image_async(image_url, product_name, timestamp, image_folder, unique_id)
                        )))

                    records.append((unique_id, current_date, page_title, product_name, description, kt, price, diamond_weight, additional_info_str))
                    sheet.append([current_date, page_title, product_name, description, kt, price, diamond_weight, time_only, image_url, additional_info_str])
                            
                # Process image downloads
                for row_num, unique_id, task in image_tasks:
                    try:
                        image_path = await asyncio.wait_for(task, timeout=60)
                        if image_path != "N/A":
                            try:
                                img = ExcelImage(image_path)
                                img.width, img.height = 100, 100
                                sheet.add_image(img, f"D{row_num}")
                            except Exception as e:
                                logging.error(f"Error embedding image: {e}")
                                image_path = "N/A"
                        for i, record in enumerate(records):
                            if record[0] == unique_id:
                                records[i] = (record[0], record[1], record[2], record[3], image_path, record[5], record[6], record[7], record[8])
                                break
                    except asyncio.TimeoutError:
                        logging.warning(f"Image download timed out for row {row_num}")
                        
                load_more_clicks += 1
                all_records.extend(records)
                wb.save(file_path)
                
        except Exception as e:
            logging.error(f"Error during scraping: {str(e)}")
            wb.save(file_path)
        finally:
            if page: await page.close()
            if browser: await browser.close()

    wb.save(file_path)
    log_event(f"Data saved to {file_path}")
    with open(file_path, "rb") as file:
        base64_encoded = base64.b64encode(file.read()).decode("utf-8")

    insert_into_db(all_records)
    update_product_count(len(all_records))

    return base64_encoded, filename, file_path