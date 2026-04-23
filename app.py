from flask import Flask, render_template, request, jsonify
import requests
import json
import re
import os
from datetime import datetime, date

app = Flask(__name__)

# Config
EXPRESS_API_ALL_BUILDER_VISITS = "https://render-backend-5sur.onrender.com/api/builder-visits?view=all"
JS_FILE_PATH = "cardsData.js"

def fetch_json_with_retry(url, timeout=20, retries=1):
    last_error = "Unknown error"
    for _ in range(retries + 1):
        try:
            res = requests.get(url, timeout=timeout)
            if res.status_code == 200:
                return res.json(), None
            # Treat missing optional endpoints as non-fatal.
            if res.status_code == 404:
                return [], f"{url} returned 404"
            last_error = f"{url} returned HTTP {res.status_code}"
        except requests.RequestException as e:
            last_error = f"{url} request failed: {e}"
    return [], last_error

def get_highest_id(js_content):
    ids = re.findall(r'["\']?id["\']?\s*:\s*(\d+)', js_content)
    if ids:
        return max([int(i) for i in ids])
    return 0

def to_js_literal(value, indent=0):
    spacer = " " * indent
    if isinstance(value, dict):
        parts = []
        for k, v in value.items():
            key = str(k)
            # Always quote keys for consistency with JSON format
            key_out = json.dumps(key)
            parts.append(f"{spacer}  {key_out}: {to_js_literal(v, indent + 2)}")
        return "{\n" + ",\n".join(parts) + f"\n{spacer}}}"
    if isinstance(value, list):
        if not value:
            return "[]"
        parts = [f"{spacer}  {to_js_literal(v, indent + 2)}" for v in value]
        return "[\n" + ",\n".join(parts) + f"\n{spacer}]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))

def extract_min_price_lakhs(box_price_str):
    if not box_price_str: return float('inf')
    box_price_str = str(box_price_str).lower().replace(',', '')
    
    matches = re.finditer(r'([\d\.]+)\s*(cr|crore|lac|lakh)?', box_price_str)
    min_val = float('inf')
    
    for match in matches:
        val_str = match.group(1)
        unit = match.group(2)
        try:
            val = float(val_str)
            if val == 0: continue
            
            if unit in ['cr', 'crore']:
                lakhs = val * 100
            elif unit in ['lac', 'lakh']:
                lakhs = val
            else:
                lakhs = val * 100 if val < 100 else val / 100000
            
            if lakhs < min_val:
                min_val = lakhs
        except:
            pass
            
    return min_val

def normalize_text(value):
    return str(value or "").strip()

def canonical_location_key(value):
    text = normalize_text(value).lower()
    text = re.sub(r'[-_/]+', ' ', text)
    text = re.sub(r'\b(road|rd)\b', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def sanitize_range_text(value):
    text = normalize_text(value)
    if not text:
        return ""
    text = text.replace("..", ".")
    text = re.sub(r'(?<=\d)\s*-\s*-(?=\d)', ' - ', text)
    text = re.sub(r'(?<=\d)\s*--\s*(?=\d)', ' - ', text)
    text = re.sub(r'\s{2,}', ' ', text).strip()
    return text

def clean_custom_label(value):
    label = normalize_text(value)
    if not label:
        return ""
    if label.lower() in {"other", "others", "na", "n/a", "-", "--"}:
        return ""
    return label

def looks_like_floor_marker(value):
    text = normalize_text(value).lower()
    if not text:
        return False
    if re.match(r'^\d+(st|nd|rd|th)?\s*floor$', text):
        return True
    if re.match(r'^(ground|basement|upper\s*ground|lower\s*ground)\s*floor?$', text):
        return True
    return False

def is_truthy_type(value, needle):
    return needle.lower() in normalize_text(value).lower()

def infer_category_and_type(parent_dev_type, prop):
    raw_type = normalize_text(prop.get("type"))
    raw_category = normalize_text(prop.get("category"))
    raw_size = normalize_text(prop.get("size"))
    raw_floor = normalize_text(prop.get("floor"))
    parent = normalize_text(parent_dev_type)

    joined = " ".join([raw_type, raw_category, raw_size, raw_floor, parent]).lower()

    if "plot" in joined:
        return "Plot", "Residential"
    if "duplex" in joined or "triplex" in joined:
        return "Duplex", "Residential"
    if "penthouse" in joined or "pent house" in joined:
        return "Penthouse", "Residential"
    if "commercial" in joined:
        return "Commercial", "Commercial"

    if parent.lower() == "commercial":
        return "Commercial", "Commercial"
    if parent.lower() == "mixed":
        # For mixed rows without explicit tags, default apartment bucket.
        return "Apartments", "Residential"

    return "Apartments", "Residential"

def infer_bhk_label(prop_type, category, prop):
    raw_size = normalize_text(prop.get("size"))
    raw_floor = normalize_text(prop.get("floor"))
    joined = f"{raw_floor} {raw_size}".lower()

    if prop_type == "Commercial" or category == "Commercial":
        # Keep explicit custom labels (e.g. Corporate House) instead of collapsing to Offices.
        custom_candidates = [
            prop.get("unitType"),
            prop.get("customType"),
            prop.get("subType"),
            prop.get("subtype"),
            raw_floor,
            raw_size,
            prop.get("category")
        ]
        for candidate in custom_candidates:
            label = clean_custom_label(candidate)
            if not label:
                continue
            label_lower = label.lower()
            if "office" in label_lower:
                return "Offices"
            if "showroom" in label_lower:
                return "Showrooms"
            if label_lower == "commercial":
                continue
            if candidate == raw_floor and looks_like_floor_marker(label):
                continue
            if label_lower not in {"other", "others"}:
                return label

        if "office" in joined:
            return "Offices"
        if "showroom" in joined:
            return "Showrooms"
        if "other" in joined:
            return "Other"
        # Fallback for commercial rows when no stronger signal exists.
        return "Offices"

    if raw_size:
        size_lower = raw_size.lower()
        if "bhk" in size_lower:
            return raw_size
        if raw_size.replace(".", "").isdigit():
            return f"{raw_size} BHK"
        return raw_size

    return "N/A"

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/drafts', methods=['GET'])
def get_drafts():
    all_data = []
    fetch_success = False
    fetch_details = []

    # Primary source: all builder visits, including Level 2 approved records.
    builder_visits, builder_visits_error = fetch_json_with_retry(EXPRESS_API_ALL_BUILDER_VISITS, timeout=20, retries=1)
    if builder_visits:
        all_data.extend(builder_visits)
        fetch_success = True
    if builder_visits_error:
        fetch_details.append(builder_visits_error)

    unique_visits = {v['_id']: v for v in all_data}.values()

    drafts = []
    for v in unique_visits:
        is_approved = v.get("approval", {}).get("level2", {}).get("status", "") == "Approved"
        sub_date = v.get("submittedAt") or v.get("createdAt")

        nested_categories = {}
        min_price_lakhs = float('inf')
        types_set = set()
        parent_dev_type = normalize_text(v.get("developmentType") or v.get("type") or "")
        
        for p in v.get("propertySizes", []):
            cat, t = infer_category_and_type(parent_dev_type, p)
            types_set.add(t)

            if cat not in nested_categories:
                nested_categories[cat] = []
                
            bhk_str = infer_bhk_label(t, cat, p)
            
            bp = sanitize_range_text(p.get("boxPrice", ""))
            sqft_value = sanitize_range_text(p.get("sqft", ""))
            pm = extract_min_price_lakhs(bp)
            if pm < min_price_lakhs: min_price_lakhs = pm
            
            # Prevent duplicate sizes in same category (basic check)
            exists = False
            for existing in nested_categories[cat]:
                if existing['bhk'] == bhk_str and existing['sqft'] == sqft_value and existing['price'] == bp:
                    exists = True
            if not exists:
                nested_categories[cat].append({
                    "bhk": bhk_str,
                    "sqft": sqft_value,
                    "sqftType": "SuperBuilt-up",
                    "price": bp
                })

        # Determine overall project type
        has_comm = "Commercial" in nested_categories
        has_res = "Apartments" in nested_categories
        has_pent = "Penthouse" in nested_categories
        has_duplex = "Duplex" in nested_categories
        has_plot = "Plot" in nested_categories

        categories_count = len(nested_categories)
        
        if parent_dev_type.lower() == "commercial":
            dType = "Commercial"
        elif parent_dev_type.lower() == "residential":
            dType = "Residential"
        elif categories_count > 1:
            dType = "Mixed"
        elif has_comm:
            dType = "Commercial"
        elif has_res:
            dType = "Residential"
        elif has_pent:
            dType = "Penthouse"
        elif has_duplex:
            dType = "Duplex"
        elif has_plot:
            dType = "Plot"
        else:
            dType = "Mixed" if "Commercial" in types_set and len(types_set) > 1 else ("Commercial" if "Commercial" in types_set else "Residential")
        
        priceNum = int(min_price_lakhs) if min_price_lakhs != float('inf') else 0
        
        features = v.get("usps", [])[:]
        
        # Rounded down to nearest multiple of 5 with a '+' for amenities
        try:
            total_amenities = int(v.get("totalAmenities", 0) or 0)
            if total_amenities > 0:
                if total_amenities < 5:
                    features.append(f"{total_amenities} Amenities")
                else:
                    rounded_amenities = (total_amenities // 5) * 5
                    features.append(f"{rounded_amenities}+ Amenities")
        except:
            if v.get("totalAmenities"):
                features.append(f"{v['totalAmenities']} Amenities")

        if v.get("allotedCarParking"): features.append(f"{v['allotedCarParking']} Alloted Car Parking")

        # Ready to move logic (based on stage or date within 3 months)
        is_ready = False
        if normalize_text(v.get("stageOfConstruction")).lower() == "ready to move":
            is_ready = True
        else:
            comp_date_str = v.get("expectedCompletionDate") # Format "YYYY-MM"
            if comp_date_str and len(comp_date_str) >= 7:
                try:
                    comp_year = int(comp_date_str[:4])
                    comp_month = int(comp_date_str[5:7])
                    today = date.today()
                    
                    # Calculate months difference
                    diff_months = (comp_year - today.year) * 12 + (comp_month - today.month)
                    
                    # If completion date is in the past OR within next 3 months
                    if diff_months <= 3:
                        is_ready = True
                except:
                    pass
        
        if is_ready:
            features.insert(0, "Ready to Move")

        drafts.append({
            "id": str(v.get('_id')),
            "date": sub_date,
            "isApproved": is_approved,
            "projectName": v.get("projectName", "Unknown"),
            "type": dType,
            "priceNum": priceNum,
            "propertyLocation": v.get("location", ""),
            "remarks": v.get("remarks") or v.get("remark", ""),
            "features": list(dict.fromkeys(features)), # deduplicate features
            "nestedCategories": nested_categories
        })

    drafts.sort(key=lambda x: str(x['date']) if x['date'] else "", reverse=True)

    # Calculate existing locations from cardsData.js safely using a better regex
    existing_locations = {}
    try:
        with open(JS_FILE_PATH, 'r', encoding='utf-8') as f:
            content = f.read()
        # Matches location: "Thaltej-17" or "location": "Thaltej-17"
        loc_matches = re.findall(r'["\']?location["\']?\s*:\s*["\']([^"\']+)["\']', content)
        for loc in loc_matches:
            base = re.sub(r'-\d+$', '', loc, flags=re.IGNORECASE)
            key = canonical_location_key(base)
            if not key:
                continue

            match = re.search(r'-(\d+)$', loc)
            suffix_num = int(match.group(1)) if match else 0
            if suffix_num > 0:
                existing_locations[key] = max(existing_locations.get(key, 0), suffix_num)
            else:
                existing_locations[key] = existing_locations.get(key, 0) + 1
    except FileNotFoundError:
        pass

    return jsonify({
        "status": "live" if fetch_success else "error",
        "data": drafts,
        "existingLocations": existing_locations,
        "message": "Live drafts fetched" if fetch_success else "Could not fetch live drafts from upstream API",
        "details": fetch_details
    })

@app.route('/api/generate-card', methods=['POST'])
def generate_card():
    new_card = request.json

    try:
        current_highest_id = int(new_card.get('currentHighestId', 0))
    except (ValueError, TypeError):
        current_highest_id = 0
        
    next_id = current_highest_id + 1

    images_list = [img.strip() for img in new_card.get("images", "").split(',') if img.strip()]
    incoming_features = new_card.get("features", [])
    if isinstance(incoming_features, list):
        features_list = [str(f).replace("\\n", "\n").strip() for f in incoming_features if str(f).strip()]
    else:
        raw_features = str(incoming_features).replace("\\n", "\n")
        features_list = [f.strip() for f in raw_features.splitlines() if f.strip()]
    
    nested_categories = new_card.get("nestedCategories", {})

    card_obj = {
        "id": next_id,
        "type": new_card.get("type", "Residential"),
        "latest": new_card.get("latest", ""),
        "location": new_card.get("location", ""),
        "price": int(float(new_card.get("price", 0) or 0)),
        "soldOut": False,
        "images": images_list,
        "propertyLocation": new_card.get("propertyLocation", ""),
        "schemeName": new_card.get("schemeName", ""),
        "features": features_list,
        "nestedCategories": nested_categories,
    }

    # Generate clean object without leading/trailing commas; frontend will handle insertion
    js_str = to_js_literal(card_obj, 2)
    
    return jsonify({
        "success": True, 
        "js_snippet": js_str, 
        "id": next_id
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=True)
