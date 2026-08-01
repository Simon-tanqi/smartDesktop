#!/usr/bin/env python
# -*- coding: utf-8 -*-

import requests
import json
import time

def test_backend_api():
    """Test the backend API to verify the fix is working"""
    
    try:
        # Wait for Flask to be ready
        for i in range(5):
            try:
                response = requests.get('http://127.0.0.1:5000/', timeout=2)
                if response.status_code == 200:
                    print("Flask app is ready!")
                    break
            except:
                print(f"Waiting for Flask to start... ({i+1}/5)")
                time.sleep(2)
        else:
            print("Flask app not ready, will try anyway")
        
        # Test a simple chat request that would trigger the routing logic
        test_data = {
            'message': 'test message',
            'session_id': 'test_session_001'
        }
        
        response = requests.post(
            'http://127.0.0.1:5000/chat', 
            json=test_data,
            timeout=30
        )
        
        if response.status_code == 200:
            result = response.json()
            print(f"✅ Chat API test success: {result.get('success', False)}")
            return True
        else:
            print(f"❌ Chat API test failed: HTTP {response.status_code}")
            print(f"Response: {response.text}")
            return False
            
    except Exception as e:
        print(f"❌ Test error: {e}")
        return False

if __name__ == "__main__":
    success = test_backend_api()
    if success:
        print("\n🎉 Backend fix verification successful!")
    else:
        print("\n😞 Backend fix verification failed.")