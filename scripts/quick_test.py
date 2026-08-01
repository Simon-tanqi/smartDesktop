#!/usr/bin/env python
import requests
import json

try:
    response = requests.post(
        'http://127.0.0.1:8765/api/chat',
        json={'message': 'Test message to verify regex fix', 'session_id': 'test_session_001'},
        timeout=30
    )
    print(f"Status: {response.status_code}")
    print(f"Response: {response.text}")
except Exception as e:
    print(f"Error: {e}")