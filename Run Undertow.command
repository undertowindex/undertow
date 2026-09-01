#!/bin/bash
cd "/Users/micahuk/undertow" || { echo "Could not find ~/undertow"; read -p "Press Enter to close..."; exit 1; }
echo "Undertow: running full Undertow Index (FRED, dealer gamma, boardroom debate, Glint section)."
echo "This calls several paid APIs and will send the configured report email. This can take a few minutes."
echo ""
/usr/local/bin/python3 undertow.py
echo ""
echo "Undertow run finished (see above for success/errors)."
read -p "Press Enter to close this window..."
