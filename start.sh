#!/bin/bash
# Production entry point; gunicorn.conf.py sets the worker model.
exec gunicorn app:server
