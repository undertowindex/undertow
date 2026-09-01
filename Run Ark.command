#!/bin/bash
cd "/Users/micahuk/undertow" || { echo "Could not find ~/undertow"; read -p "Press Enter to close..."; exit 1; }
source ~/.bash_profile 2>/dev/null
export RESEND_API_KEY="$(/usr/libexec/PlistBuddy -c 'Print :EnvironmentVariables:RESEND_API_KEY' ~/Library/LaunchAgents/com.undertow.ark.plist 2>/dev/null)"
echo "Ark: manual advisory run (reads Undertow's last published signal, proposes hedges/quality-survivor picks if warranted)."
echo "This does not place any real or paper trades. It may send an email if there's something to report."
echo ""
/usr/local/bin/python3 ark.py
echo ""
echo "Ark run finished (see above for details)."
read -p "Press Enter to close this window..."
