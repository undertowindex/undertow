#!/bin/bash
cd "/Users/micahuk/glint" || { echo "Could not find ~/glint"; read -p "Press Enter to close..."; exit 1; }
echo "Glint: fetching UK + US stock fundamentals, this takes 2-3 minutes..."
/usr/local/bin/python3 -m glint.main --out latest_report.txt --out-html latest_report.html
if [ $? -eq 0 ]; then
  open latest_report.html
  echo ""
  echo "Done. Report opened in your browser."
else
  echo ""
  echo "Something went wrong — see the errors above."
fi
read -p "Press Enter to close this window..."
