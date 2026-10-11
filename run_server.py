import os, subprocess, sys
os.environ['INTEGRATION_MODE'] = 'mock'
os.environ['OPENSEO_MOCK_MODE'] = '1'
subprocess.run([sys.executable, '-m', 'uvicorn', 'api.main:app', '--port', '8000'], cwd=r'D:\Projects\OpenSEO AI\src')

