name: Alert Monitor System

on:
  workflow_dispatch:
  repository_dispatch:
    types: [trigger_monitor]
  push:
    branches: [ "main", "master" ]

jobs:
  monitor:
    runs-on: ubuntu-latest
    
    steps:
    - name: Checkout repository
      uses: actions/checkout@v4
      
    - name: Set up Python
      uses: actions/setup-python@v5
      with:
        python-version: '3.11'
        
    - name: Install dependencies
      run: |
        python -m pip install --upgrade pip
        pip install -r requirements.txt
        
    - name: Install Playwright Browsers & Dependencies
      run: |
        sudo apt-get update
        # Explicitly install modern Ubuntu 24.04 libraries
        sudo apt-get install -y libasound2t64 libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libdbus-1-3 libexpat1 libfontconfig1 libgbm1 libglib2.0-0 libpango-1.0-0 libpangocairo-1.0-0 libstdc++6 libx11-6 libx11-xcb1 libxcb1 libxcomposite1 libxcursor1 libxdamage1 libxext1 libxfixes1 libxi6 libxrandr2 libxrender1 libxss1 libxtst6
        playwright install chromium
        
    - name: Restore state file memory (Cache)
      uses: actions/cache/restore@v4
      with:
        path: state.json
        key: state-memory-${{ github.run_id }}
        restore-keys: |
          state-memory-
          
    - name: Run monitoring script
      env:
        TWILIO_ACCOUNT_SID: ${{ secrets.TWILIO_ACCOUNT_SID }}
        TWILIO_AUTH_TOKEN: ${{ secrets.TWILIO_AUTH_TOKEN }}
        MY_PHONE: ${{ secrets.MY_PHONE }}
        TWILIO_FROM: ${{ secrets.TWILIO_FROM }}
        FB_PAGE_1: ${{ secrets.FB_PAGE_1 }}
        FB_PAGE_2: ${{ secrets.FB_PAGE_2 }}
        FB_PAGE_3: ${{ secrets.FB_PAGE_3 }}
        STOP_ALERTS: ${{ vars.STOP_ALERTS }}
      run: python main.py
      
    - name: Save state file memory (Cache)
      uses: actions/cache/save@v4
      if: always()
      with:
        path: state.json
        key: state-memory-${{ github.run_id }}

    - name: Upload Visible Report (Artifact)
      if: always()
      uses: actions/upload-artifact@v4
      with:
        name: execution-report
        path: state.json
        retention-days: 7

    - name: Print Report to Console
      if: always()
      run: |
        if [ -f state.json ]; then
          echo "--- LATEST LOGS ---"
          cat state.json
        fi
