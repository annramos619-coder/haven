#!/bin/bash
# Run this once to save your Gemini API key.
# Your key will NOT appear on screen as you type it.
echo ""
echo -n "Enter your GEMINI_API_KEY: "
read -rs GEMINI_API_KEY
echo ""

if [ -z "$GEMINI_API_KEY" ]; then
  echo "ERROR: No key entered."
  exit 1
fi

# Write to ~/.zshrc (strip any prior GEMINI_API_KEY export first)
grep -v 'export GEMINI_API_KEY=' ~/.zshrc > /tmp/zshrc_tmp && mv /tmp/zshrc_tmp ~/.zshrc
echo "export GEMINI_API_KEY=\"$GEMINI_API_KEY\"" >> ~/.zshrc

# Load immediately
export GEMINI_API_KEY="$GEMINI_API_KEY"

echo "Key saved to ~/.zshrc and loaded into this session."
echo "Key starts with: ${GEMINI_API_KEY:0:8}..."
echo ""
echo "Now run:  python3 app.py"
