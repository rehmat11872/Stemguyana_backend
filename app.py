import eventlet
# eventlet.monkey_patch()
import openai
from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO
import fitz  # PyMuPDF
import requests
from dotenv import load_dotenv
# from nltk.tokenize import sent_tokenize
import time
import threading
import re
import os


# Load environment variables from .env file
load_dotenv()

# Load OpenAI API key from environment variable
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise ValueError("OpenAI API key not found in environment variable OPENAI_API_KEY")

openai.api_key = OPENAI_API_KEY

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')
# socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading', logger=True, engineio_logger=True)


class TextToSpeechApp:
    def __init__(self, pdf_path, speech_speed=1.0):
        self.pdf_path = pdf_path
        self.is_paused = False
        self.sentences = []
        self.current_sentence_index = 0
        self.audio_thread = None
        self.speech_speed = speech_speed
        self.resume_callback = None
        self.url_detected = False

        self.resume_requested = False 

    def set_pdf_path(self, pdf_path):
        self.pdf_path = pdf_path

    def extract_text_from_pdf(self):
        if self.pdf_path:
            try:
                doc = fitz.open(self.pdf_path)
                text = ""
                for page_number in range(doc.page_count):
                    page = doc[page_number]
                    text += page.get_text()
                os.remove(self.pdf_path)
                return text
            except Exception as e:
                print(f"Error extracting text from PDF: {e}")
                return ""
        else:
            return ""
        
    def handle_link_detection_result(self, url_detected):
        self.url_detected = url_detected
        if self.resume_callback:
            self.resume_callback(not url_detected)

    def is_question(self, sentence):
        sentence = sentence.strip().lower()
        question_starters = ['who', 'what', 'when', 'where', 'why', 'how', 'give an', 'examples']
        
        # Exclude specified words from triggering a pause
        exclude_words = ['whoever', 'whatever', 'whenever', 'wherever', 'whyever', 'however']

        return (
            sentence.endswith('?') or
            (sentence.startswith(tuple(question_starters)) and not sentence.endswith('!') and not any(word in sentence for word in exclude_words))
        )

    # def play_text_to_speech(self):
    #     self.sentences = sent_tokenize(self.extract_text_from_pdf())
    #     self.is_paused = False
    #     self.current_sentence_index = 0
    #     socketio.emit('status', {'status': 'playing'})
    #     self.play_audio_chunk()

    def pause_text_to_speech(self):
        self.is_paused = True

    def resume_text_to_speech(self):
        self.is_paused = False
        if self.current_sentence_index < len(self.sentences):
            self.play_audio_chunk()

    def play_audio_chunk(self):
        try:
            while self.current_sentence_index < len(self.sentences):
                if self.is_paused:
                    socketio.emit('status', {'status': 'paused'})
                    break

                sentence = self.sentences[self.current_sentence_index]

                # Check for links in the current sentence
                if self.sentence_contains_link(sentence):
                    # Emit a request for link detection
                    socketio.emit('request_link_detection', {'sentence': sentence})

                    # Set the callback for resume decision
                    self.resume_callback = self.handle_resume_decision

                    # Wait for the callback to be triggered
                    while self.resume_callback is not None:
                        eventlet.sleep(0.1)  # Adjust the sleep time as needed

                    if self.is_paused:
                        # The client handled the link detection and paused the speech
                        break
                    else:
                        # The client didn't pause the speech, continue playing
                        audio_data = self.generate_openai_tts(sentence)
                        socketio.emit('audio_chunk', {'audio_data': audio_data})


                        # Calculate sleep time based on the length of the audio_data and speech speed
                        sleep_time = len(audio_data) / (18000 * self.speech_speed)  # Adjusted for speed factor
                        eventlet.sleep(sleep_time)

                        self.current_sentence_index += 1

                        if self.is_question(sentence):
                            socketio.emit('question_result', {'is_question': True})
                            self.pause_text_to_speech()
                            socketio.emit('status', {'status': 'paused'})
                            break
                else:
                    # No link detected, continue playing
                    audio_data = self.generate_openai_tts(sentence)
                    socketio.emit('audio_chunk', {'audio_data': audio_data})


                    # Calculate sleep time based on the length of the audio_data and speech speed
                    sleep_time = len(audio_data) / (18000 * self.speech_speed)  # Adjusted for speed factor
                    # sleep_time = 0.1 / self.speech_speed
                    eventlet.sleep(sleep_time)

                    self.current_sentence_index += 1

                    if self.is_question(sentence):
                        socketio.emit('question_result', {'is_question': True})
                        self.pause_text_to_speech()
                        socketio.emit('status', {'status': 'paused'})
                        break
        except Exception as e:
            print(f"Text-to-speech error: {e}")
        finally:
            if self.current_sentence_index >= len(self.sentences):
                socketio.emit('status', {'status': 'stopped'})

    def handle_resume_decision(self, resume):
        # Resume decision callback function
        if resume:
            self.resume_requested = True
        else:
            self.pause_text_to_speech()
            socketio.emit('status', {'status': 'paused'})
        self.resume_callback = None

    def sentence_contains_link(self, sentence):
        return re.search(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', sentence)

    def filter_urls(self, text):
        # Use a regular expression to find URLs in the text
        urls = re.findall(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', text)

        # Replace URLs with an empty string
        for url in urls:
            text = text.replace(url, '')

        return text

    def stop_text_to_speech(self):
        self.is_paused = True
        self.current_sentence_index = len(self.sentences)

    def generate_openai_tts(self, text):
        try:
            headers = {
                'Authorization': f'Bearer {OPENAI_API_KEY}',
                'Content-Type': 'application/json',
            }

            # Filter out URLs from the text
            filtered_text = self.filter_urls(text)

            data = {
                'input': filtered_text,
                'voice': 'alloy',
                'model': 'tts-1',
            }

            # Send the request to OpenAI TTS API
            response = requests.post(
                'https://api.openai.com/v1/audio/speech',
                headers=headers,
                json=data
            )

            # Check if the request was successful (status code 200)
            if response.status_code == 200:
                # Assuming the response contains audio data, adjust this based on the actual OpenAI TTS response structure
                audio_data = response.content
                if audio_data is not None:
                    return audio_data
                else:
                    print("Error: Audio data is None.")
            else:
                # Handle the error, print a message, or return an appropriate value
                print(f"Error in OpenAI TTS API request: {response.text}")
                return None
        except Exception as e:
            print(f"Error generating OpenAI TTS: {e}")
            return None

    def generate_openai_chat(self, text):
        try:
            headers = {
                'Authorization': f'Bearer {OPENAI_API_KEY}',
                'Content-Type': 'application/json',
            }

            # Prompt for the question-answering model
            prompt = f"Teacher, {text}"

            data = {
                'messages': [
                    {"role": "system", "content": "you are a tutor. Please read questions and help students determine answers one question at a time."},
                    {"role": "user", "content": prompt}
                ],
                'model': 'gpt-3.5-turbo',
                'temperature': 0.2
            }

            # Send the request to OpenAI GPT-3.5 API
            response = requests.post(
                'https://api.openai.com/v1/chat/completions',  
                headers=headers,
                json=data
            )

            # Check if the request was successful (status code 200)
            if response is not None and response.status_code == 200:
                # Assuming the response contains generated text, adjust this based on the actual OpenAI response structure
                answer = response.json()['choices'][0]['message']['content']
                return answer.encode('utf-8')  # Convert to bytes
            else:
                # Print details of the error
                print(f"Error in OpenAI GPT-3 API request: {response.text if response else 'No response'}")
                return b'Error in API request'
        except Exception as e:
            print(f"Error generating OpenAI chat response: {e}")
            return b'Error in API request'
        

from urllib.parse import unquote, urlparse, parse_qs

def extract_direct_pdf_url(google_docs_viewer_url):
    # Extract the 'url' parameter from the query string
    parsed_url = urlparse(google_docs_viewer_url)
    query_params = parse_qs(parsed_url.query)
    
    # Get the 'url' parameter and unquote it
    pdf_url_encoded = query_params.get('url', [''])[0]
    pdf_url = unquote(pdf_url_encoded)
    
    return pdf_url

@app.route('/set_pdf_path', methods=['POST'])
def set_pdf_path():
    try:
        data = request.get_json()
        google_docs_viewer_url = data.get('pdfUrl')
        direct_pdf_url = extract_direct_pdf_url(google_docs_viewer_url)

        response = requests.get(direct_pdf_url)
        if response.status_code == 200:
            pdf_content = response.content
            # print(pdf_content)
            folder_path = 'pdf_files'
            os.makedirs(folder_path, exist_ok=True)

            # Assuming you want to use the last saved PDF ID as the filename
            pdf_file_path = os.path.join(folder_path, f"new_pdf.pdf")
            print(pdf_file_path, 'pdf_file_path')

            # Write the PDF content to the file
            with open(pdf_file_path, 'wb') as pdf_file:
                pdf_file.write(pdf_content)


            return jsonify({'message': 'PDF path set successfully'}), 200
        else:
            return jsonify({'error': f"Failed to fetch PDF. Status code: {response.status_code}"}), 500

    except requests.exceptions.RequestException as e:
        return jsonify({'error': f"Error fetching PDF: {e}"}), 500
    


@app.route('/link_detection_result', methods=['POST'])
def link_detection_result():
    try:
        data = request.get_json()
        url_detected = data.get('url_detected')
        text_to_speech_app.handle_link_detection_result(url_detected)
        return "Link detection result received"
    except Exception as e:
        return f"Error handling link detection result: {e}", 500



@socketio.on('play')
def handle_play():
    print('Working')
    try:
        text_to_speech_app.play_text_to_speech()
        print('Play event received')
    except Exception as e:
        print(f"Error handling play event: {e}")




@socketio.on('pause')
def handle_pause():
    try:
        text_to_speech_app.pause_text_to_speech()
    except Exception as e:
        print(f"Error handling pause event: {e}")

@socketio.on('resume')
def handle_resume():
    try:
        text_to_speech_app.resume_text_to_speech()
    except Exception as e:
        print(f"Error handling resume event: {e}")

@socketio.on('resume_decision')
def handle_resume_decision(data):
    try:
        resume = data.get('resume')
        if text_to_speech_app.resume_callback is not None:
            text_to_speech_app.resume_callback(resume)
    except Exception as e:
        print(f"Error handling resume decision: {e}")

@app.route('/stop')
def handle_stop():
    try:
        text_to_speech_app.stop_text_to_speech()
        socketio.emit('status', {'status': 'stopped'})
        return 'Speech stopped'
    except Exception as e:
        return f"Error handling stop event: {e}", 500

@socketio.on('is_question')
def handle_is_question(data):
    try:
        is_question = text_to_speech_app.is_question(data.get('sentence'))
        socketio.emit('question_result', {'is_question': is_question})
    except Exception as e:
        print(f"Error handling is_question event: {e}")

@socketio.on('submitQuestion')
def handle_submit_question(data):
    try:
        question = data.get('question')
        response = text_to_speech_app.generate_openai_chat(question)
        socketio.emit('gpt_response', {'response': response.decode('utf-8') if response else 'Error'})
    except Exception as e:
        print(f"Error handling submitQuestion event: {e}")

@app.route('/')
def index():
    return render_template('index.html')


if __name__ == "__main__":
    # pdf_path = 'test1.pdf'
    pdf_path = 'pdf_files/new_pdf.pdf'
    text_to_speech_app = TextToSpeechApp(pdf_path)
    # Use the environment variable for the OpenAI API key
    # eventlet.monkey_patch()
    socketio.run(app, host='0.0.0.0', port=5000)  
    # app.run(host='0.0.0.0', port=5000, threaded=True, debug=True) 
