from flask import Flask, render_template, request, Response
from openai import OpenAI
import requests
import json
import pymongo
import tiktoken  # For token counting
import os

app = Flask(__name__)

# Load your OpenAI API key from an environment variable or other secure location
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
MONGODB_CONNECTION_STRING = os.getenv('MONGODB_IST_MEDIA')
MONGODB_DATABASE_NAME = '1_media_demo'
MONGODB_COLLECTION_NAME = 'news'

mongo_client = pymongo.MongoClient(MONGODB_CONNECTION_STRING)
mongo_db = mongo_client[MONGODB_DATABASE_NAME]
mongo_collection = mongo_db[MONGODB_COLLECTION_NAME]

ai = OpenAI()

# Set the maximum token limit for GPT-4 (adjust if using a different variant)
MAX_TOKENS = 8192

# Reserve tokens for the assistant's reply and system prompt (if any)
RESERVED_TOKENS = 1000

# Initialize the tokenizer for GPT-4
encoding = tiktoken.encoding_for_model('gpt-4o')


def num_tokens_from_messages(messages):
    """
    Calculate the number of tokens used by a list of messages.
    """
    num_tokens = 0
    for message in messages:
        # Every message follows <|start|>{role/name}\n{content}<|end|>\n
        num_tokens += 4  # For the message format tokens
        for key, value in message.items():
            num_tokens += len(encoding.encode(value))
    num_tokens += 2  # For priming
    return num_tokens


def vector_search(embedding):
    results = mongo_collection.aggregate([
        {
            '$vectorSearch': {
                "index": 'vector_index',
                "path": 'embedding',
                "queryVector": embedding,
                "numCandidates": 20,
                "limit": 5,
            }
        },
        {
            "$project": {
                '_id' : 0,
                'text' : 1,
                'published' : 1,
                "search_score": { "$meta": "vectorSearchScore" }
            }
        }
    ])
    return list(results)


def get_relevant_documents(user_input):
    # Generate embedding for the user's input
    response = ai.embeddings.create(input=user_input, model='text-embedding-ada-002')
    user_embedding = response.data[0].embedding
    results = vector_search(user_embedding)
    relevant_docs = [ "publish date: " + str(doc['published']) + ", news:" + doc['text'] for doc in results ]
    return relevant_docs


@app.route('/')
def home():
    return render_template('index.html')


@app.route('/chat', methods=['POST'])
def chat():
    user_message = request.json.get('message')
    chat_history = request.json.get('history', [])

    # Define the system prompt
    system_prompt = {"role": "system", "content": "You manage a news repository. For everything you tell me about, add information about the publishing date of the news. Use a format like 'September 7, 2024'. If you have news from several days, please sort them from the newest to the oldest."}

    # Ensure the system prompt is at the beginning of the chat history
    if not chat_history or chat_history[0].get('role') != 'system':
        chat_history.insert(0, system_prompt)

    # Append the user's message to the chat history
    chat_history.append({"role": "user", "content": user_message})

    # Get relevant documents from MongoDB
    relevant_docs = get_relevant_documents(user_message)

    # Incorporate the context into the conversation
    if relevant_docs:
        context = "\n\n".join(relevant_docs)
        context_message = {
            "role": "system",
            "content": f"Answer the question based on the following context:\n\n{context}"
        }
        # Insert the context after the system prompt
        chat_history.insert(1, context_message)

    # Calculate the number of tokens and trim the chat history if necessary
    while True:
        num_tokens = num_tokens_from_messages(chat_history)
        if num_tokens + RESERVED_TOKENS <= MAX_TOKENS:
            break

        # Remove the oldest user-assistant message pair after the context message
        if len(chat_history) > 4:  # Ensure at least system prompt, context message, and latest user message remain
            del chat_history[2:4]  # Remove messages at indexes 2 and 3
        else:
            break

    def generate():
        # Prepare the payload for OpenAI API
        payload = {
            "model": "gpt-4o",
            "messages": chat_history,
            "stream": True  # Enable streaming
        }

        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json"
        }

        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
            stream=True  # Stream the response
        )

        if response.status_code != 200:
            error_message = response.json().get('error', {}).get('message', 'Failed to get response from OpenAI API')
            yield f"data: {json.dumps({'error': error_message})}\n\n"
            return

        assistant_reply = ''
        for line in response.iter_lines():
            if line:
                decoded_line = line.decode('utf-8')
                if decoded_line.startswith('data:'):
                    data_line = decoded_line[5:].strip()
                    if data_line == '[DONE]':
                        break
                    data_json = json.loads(data_line)
                    delta = data_json['choices'][0]['delta']
                    if 'content' in delta:
                        content = delta['content']
                        assistant_reply += content
                        # Send the new content to the client
                        yield f"data: {json.dumps({'content': content})}\n\n"
        # Append assistant's reply to the chat history
        chat_history.append({"role": "assistant", "content": assistant_reply})
        # Send the final message with updated history
        yield f"data: {json.dumps({'done': True, 'history': chat_history})}\n\n"

    return Response(generate(), mimetype='text/event-stream')

if __name__ == '__main__':
    app.run(debug=True, host="0.0.0.0", port=9494)

