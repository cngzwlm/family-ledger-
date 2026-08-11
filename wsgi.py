# PythonAnywhere / 通用 WSGI 入口
# 托管平台加载本文件，调用其中的 application（即 server.wsgi_app）。
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import wsgi_app as application
