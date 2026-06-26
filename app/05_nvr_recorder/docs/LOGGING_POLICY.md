# Logging policy

The recorder should write one monthly file.

- File example: nvr_YYYY-MM.log
- Each record should be written and closed immediately.
- If the main file cannot be appended, use a numbered fallback file.
- Example fallback: nvr_YYYY-MM-2.log

This keeps the recorder running while another PC is reading the current file.
