from flask import Flask
from threading import Thread
import os

app = Flask('')

@app.route('/')
def home():
    return "Selfbot is alive and running!"

def run():
    # Render binds dynamic ports via the PORT environment variable
    port = int(os.environ.get('PORT', 8080))
    # use Waitress or just raw Flask since this is internally just a ping-receiver
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run)
    t.start()
