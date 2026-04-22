# C2PA Soft Binding Resolution API

Complete implementation of the C2PA Soft Binding Resolution API v2.3.0 using FastAPI and MongoDB.

This API enables matching soft bindings (watermarks and fingerprints) to C2PA Manifests, providing content provenance verification for digital assets.

## Features

### Query Endpoints
- `GET /matches/byBinding` - Query manifests by soft binding value
- `POST /matches/byBinding` - Query manifests with large soft binding values
- `POST /matches/byContent` - Find manifests by uploading an asset file
- `POST /matches/byReference` - Find manifests by asset reference URL (with SSRF protection)

### Manifest Management
- `POST /manifests` - Store C2PA Manifest Store in the repository
- `GET /manifests/{manifestId}` - Retrieve C2PA Manifest Store by ID
- `DELETE /manifests/{manifestId}` - Remove manifest from repository

### Soft Binding Management
- `POST /bindings` - Associate a manifest with a soft binding value
- `PUT /bindings` - Update manifest association for a soft binding

### Receipt Management
- `GET /manifests/{manifestId}/receipts` - Get verification receipt for a manifest
- `POST /manifests/{manifestId}/receipts` - Verify a receipt against a manifest

### Service Information
- `GET /services/supportedAlgorithms` - List supported watermark and fingerprint algorithms

## Prerequisites

- Python 3.8+
- MongoDB running locally on port 27017

### Install MongoDB (if not already installed)

**macOS:**
```bash
brew tap mongodb/brew
brew install mongodb-community
brew services start mongodb-community
```

**Linux:**
```bash
sudo apt-get install mongodb
sudo systemctl start mongodb
```

**Windows:**
Download from https://www.mongodb.com/try/download/community

## Setup

1. **Install dependencies:**
```bash
pip install -r requirements.txt
```

2. **Initialize the database with sample data:**
```bash
python init_db.py
```

3. **Run the API server:**
```bash
uvicorn main:app --reload
```

The API will be available at: http://localhost:8000

## API Documentation

Once the server is running, visit:
- **Interactive API docs (Swagger UI):** http://localhost:8000/docs
- **Alternative docs (ReDoc):** http://localhost:8000/redoc

## Testing the Endpoints

### 1. List Supported Algorithms
```bash
curl http://localhost:8000/services/supportedAlgorithms
```

### 2. Query by Soft Binding (GET)
```bash
# Using the sample data from init_db.py
curl "http://localhost:8000/matches/byBinding?alg=example.watermark.v1&value=d2F0ZXJtYXJrX3ZhbHVlXzEyMw%3D%3D&maxResults=10"
```

### 3. Query by Large Soft Binding (POST)
```bash
curl -X POST "http://localhost:8000/matches/byBinding?maxResults=10" \
  -H "Content-Type: application/json" \
  -d '{
    "alg": "example.watermark.v1",
    "value": "d2F0ZXJtYXJrX3ZhbHVlXzEyMw=="
  }'
```

### 4. Query by Content (File Upload)
```bash
curl -X POST "http://localhost:8000/matches/byContent?alg=example.watermark.v1&maxResults=10" \
  -H "Content-Type: image/jpeg" \
  -F "file=@/path/to/image.jpg"
```

### 5. Query by Reference URL
```bash
curl -X POST "http://localhost:8000/matches/byReference?alg=example.watermark.v1" \
  -H "Content-Type: application/json" \
  -d '{
    "referenceUrl": "https://example.com/asset.jpg",
    "assetLength": 1048576,
    "assetType": "image/jpeg"
  }'
```

### 6. Store a Manifest
```bash
# Store a C2PA manifest (binary format)
curl -X POST "http://localhost:8000/manifests?returnReceipt=true" \
  -H "Content-Type: application/c2pa" \
  --data-binary "@manifest.c2pa"
```

### 7. Get Manifest by ID
```bash
curl http://localhost:8000/manifests/urn:uuid:12345678-1234-1234-1234-123456789abc
```

### 8. Create Soft Binding Association
```bash
curl -X POST http://localhost:8000/bindings \
  -H "Content-Type: application/json" \
  -d '{
    "bindingValue": "d2F0ZXJtYXJrX3ZhbHVlXzEyMw==",
    "manifestId": "urn:uuid:12345678-1234-1234-1234-123456789abc"
  }'
```

### 9. Update Soft Binding Association
```bash
curl -X PUT http://localhost:8000/bindings \
  -H "Content-Type: application/json" \
  -d '{
    "bindingValue": "d2F0ZXJtYXJrX3ZhbHVlXzEyMw==",
    "manifestId": "urn:uuid:87654321-4321-4321-4321-cba987654321"
  }'
```

### 10. Get Verification Receipt
```bash
curl http://localhost:8000/manifests/urn:uuid:12345678-1234-1234-1234-123456789abc/receipts
```

### 11. Verify Receipt
```bash
curl -X POST http://localhost:8000/manifests/urn:uuid:12345678-1234-1234-1234-123456789abc/receipts \
  -H "Content-Type: application/json" \
  -d '{
    "@context": {
      "c2pa": "https://c2pa.org/ns/",
      "receipt": "https://c2pa.org/ns/manifest-receipt#"
    },
    "@type": "org.c2pa.manifest-receipt",
    "repository": {
      "uri": "https://repo.example.org",
      "manifestId": "urn:uuid:12345678-1234-1234-1234-123456789abc"
    },
    "anchor": {
      "uri": "https://repo.example.org/anchors/123456",
      "proof": {
        "alg": "ES256",
        "value": "BASE64URL_PROOF_VALUE"
      }
    }
  }'
```

### 12. Delete Manifest
```bash
curl -X DELETE http://localhost:8000/manifests/urn:uuid:12345678-1234-1234-1234-123456789abc
```

## Project Structure

```
c2pa soft bindings/
├── main.py                    # FastAPI application and routes
├── models.py                  # Pydantic models
├── database.py                # MongoDB connection
├── config.py                  # Configuration settings
├── init_db.py                 # Database initialization script
├── requirements.txt           # Python dependencies
├── specification.json         # OpenAPI specification
└── README.md                  # This file
```

## Database Collections

- **manifests** - Stores C2PA manifest data
- **soft_bindings** - Stores soft binding to manifest mappings
- **supported_algorithms** - Lists supported watermark and fingerprint algorithms

## Configuration

Create a `.env` file to customize settings:
```env
MONGODB_URL=mongodb://localhost:27017
DATABASE_NAME=c2pa_soft_bindings
API_TITLE=C2PA Soft Binding Resolution API
API_VERSION=1.1.0
```

## Error Handling

The API returns standard HTTP status codes:
- **200**: Successful operation
- **204**: Successful operation with no content
- **400**: Invalid request (bad parameters, invalid format)
- **403**: Client not allowed to perform operation
- **404**: Resource not found
- **414**: URI too long (use POST instead of GET)
- **415**: Unsupported media type
- **500**: Internal server error

## Implementation Notes

### Placeholder Functionality

Some endpoints contain placeholder implementations that require algorithm-specific code:

1. **Soft Binding Extraction** (`/matches/byContent`, `/matches/byReference`):
   - Currently returns empty results
   - Requires implementing actual watermark/fingerprint extraction algorithms
   - Each algorithm (from the supported algorithms list) needs specific extraction logic

2. **C2PA Manifest Parsing** (`/manifests`):
   - Currently stores manifests as-is without parsing
   - In production, should use c2pa-python or c2pa-rs to parse and validate manifests
   - Should extract the active manifest ID from the JUMBF structure

3. **Receipt Verification** (`/manifests/{manifestId}/receipts`):
   - Currently uses simplified verification
   - In production, should cryptographically verify the receipt proof
   - Should validate signatures and anchor proofs

## Next Steps

To make this production-ready:
- Integrate c2pa-python library for manifest parsing and validation
- Implement actual watermark/fingerprint extraction algorithms
- Add OAuth2 authentication as specified in the OpenAPI spec
- Enhance SSRF protection (domain allowlist, cloud metadata endpoint blocking)
- Add rate limiting
- Add logging and monitoring
- Implement proper receipt storage and cryptographic verification
- Add database indexes for performance optimization
