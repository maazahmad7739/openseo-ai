$env:INTEGRATION_MODE = 'mock'
$env:OPENSEO_MOCK_MODE = '1'
Set-Location 'D:\Projects\OpenSEO AI\src'
python -m uvicorn api.main:app --port 8000

