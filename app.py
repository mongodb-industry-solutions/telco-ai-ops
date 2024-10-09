from flask import Flask, render_template, request, Response
from openai import OpenAI
import requests
import json
import pymongo
import tiktoken  # For token counting
import os, re, pycountry, datetime

app = Flask(__name__)

# Load your OpenAI API key from an environment variable or other secure location
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
MONGODB_CONNECTION_STRING = os.getenv('MONGODB_IST_MEDIA')
MONGODB_DATABASE_NAME = '1_media_demo'
MONGODB_COLLECTION_NAME = 'news'

mongo_client = pymongo.MongoClient(MONGODB_CONNECTION_STRING)
mongo_db = mongo_client[MONGODB_DATABASE_NAME]
mongo_collection = mongo_db[MONGODB_COLLECTION_NAME]

# raw data for pre-aggregations
access_log_collection = mongo_db["access_log"]

ai = OpenAI()

# Set the maximum token limit for GPT-4 (adjust if using a different variant)
MAX_TOKENS = 8192

# Reserve tokens for the assistant's reply and system prompt (if any)
RESERVED_TOKENS = 1000

# Initialize the tokenizer for GPT-4
encoding = tiktoken.encoding_for_model('gpt-4o')


def parse_iso_date(match):
    date_str = match.group(1)
    # Remove 'Z' at the end if present
    if date_str.endswith('Z'):
        date_str = date_str[:-1]
    # Replace 'T' with space to match strptime format
    date_str = date_str.replace('T', ' ')
    # Return a placeholder
    return '"__ISODate__{}__"'.format(date_str)


def replace_iso_dates(obj):
    if isinstance(obj, dict):
        return {k: replace_iso_dates(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [replace_iso_dates(v) for v in obj]
    elif isinstance(obj, str) and obj.startswith('__ISODate__'):
        date_str = obj.strip('__ISODate__')
        dt = datetime.strptime(date_str, '%Y-%m-%d %H:%M:%S')
        return dt
    else:
        return obj


def process_pipeline(pipeline_str):
    # Replace ISODate('...') with a placeholder
    pipeline_str = re.sub(r'ISODate\(\s*[\'"](.+?)[\'"]\s*\)', parse_iso_date, pipeline_str)
    # Replace single quotes with double quotes
    pipeline_str = pipeline_str.replace("'", '"')
    # Parse the JSON string
    pipeline = json.loads(pipeline_str)
    # Replace placeholders with datetime objects
    pipeline = replace_iso_dates(pipeline)
    return pipeline


def gen(text):
    prompt = f"""

    Ich brauche Dich als MongoDB aggregation pipeline builder. Du
    übersetzt jeweils meine natürlichsprachlichen Abfragen in eine
    passende pipeline, die dann MongoDB-Daten zurückliefert, die in
    einer RAG Architektur zu leistungsfähigen ergebnissen führt. das
    zugrundeliegende MongoDB-Datenschema lautet wie folgt:

    {{
      _id: ObjectId('668551649101ed2d266dd505'),
      timestamp: ISODate('2024-07-03T13:25:56.391Z'),
      path: '/backstage',
      ip: '104.30.134.186',
      city: 'Copenhagen',
      country: 'DK'
    }}

    Bitte liefere stets nur das Javascript zurück, nix drum herum. Auch kein
    ```javascript oder ähnliches.

    Ich wiederhole: Nur die Javascript Liste zurückgeben, kein ```oder ´´´, keinerlei
    andere prefixe oder suffixe.
    {text}
    """

    try:
        response = ai.chat.completions.create(
            model = "gpt-4o",
            messages = [
                { "role" : "system", "content" : "You are a MongoDB query creation assistant." },
                { "role" : "user", "content" : prompt }
            ],
            max_tokens = 2000,
            n = 1,
            temperature = 0.7
        )
        pipeline = response.choices[0].message.content.strip()
        print(pipeline)
        return pipeline

    except Exception as e:
        print(f"An error occurred: {e}")
        return None


# pre-aggregated input for the LLM
pipeline_countries = [
    {
        "$match": {
            "country": { "$exists": True },
            "city": { "$exists": True }
        }
    },
    {
        "$group": {
            "_id": {
                "country": "$country",
                "city": "$city"
            },
            "city_access_count": { "$sum": 1 }
        }
    },
    {
        "$sort": { "city_access_count": -1 }
    },
    {
        "$group": {
            "_id": "$_id.country",
            "access_count": { "$sum": "$city_access_count" },
            "top_cities": {
                "$push": {
                    "city": "$_id.city",
                    "access_count": "$city_access_count"
                }
            }
        }
    },
    {
        "$sort": { "access_count": -1 }
    },
    {
        "$limit": 12
    },
    {
        "$project": {
            "_id": 1,
            "access_count": 1,
            "top_cities": {
                "$slice": ["$top_cities", 5]
            }
        }
    }
]

pipeline_paths = [
    {
        "$group": {
            "_id": "$path",
            "access_count": { "$sum": 1 }
        }
    },
    {
        "$sort": { "access_count": -1 }
    },
    {
        "$limit": 10
    },
    {
        "$project": {
            "_id": 1,
            "access_count": 1
        }
    }
]


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
    response = ai.embeddings.create(input = user_input, model = 'text-embedding-ada-002')
    user_embedding = response.data[0].embedding
    results = vector_search(user_embedding)
    relevant_docs = [ "publish date: " + str(doc['published']) + ", news:" + doc['text'] for doc in results ]
    return relevant_docs


def get_country_name(iso_code):
    country = pycountry.countries.get(alpha_2=iso_code)
    return country.name if country else "Unknown"


def get_logs(user_input):
    try:
        #country_stats = list(access_log_collection.aggregate(pipeline_countries))
        #for entry in country_stats: # add full country names
        #    entry['country'] = get_country_name(entry['_id'])
        #return str(country_stats)
        pipeline = gen(user_input)
        if pipeline:
            try:
                pipeline = process_pipeline(pipeline)
                print(pipeline)
                return str(list(access_log_collection.aggregate(pipeline)))
            except:
                return ""
        else:
            return ""

    except Exception as e:
        print("get_logs(): " + str(e))
        exit(1)


@app.route('/')
def home():
    return render_template('index.html')


@app.route('/chat', methods=['POST'])
def chat():
    user_message = request.json.get('message')
    chat_history = request.json.get('history', [])

    # Define the system prompt
    system_prompt = { "role": "system", "content": "You are a telco network AI-OPS chatbot. I can communicate with you about the health of the network and ask about current stats. You will find latest aggregated data in your context prompt. Please use that to provide meaingful answers, including projections into the future where appropriate or asked for." }

    # Ensure the system prompt is at the beginning of the chat history
    if not chat_history or chat_history[0].get('role') != 'system':
        chat_history.insert(0, system_prompt)

    # Append the user's message to the chat history
    chat_history.append({ "role": "user", "content": user_message })

    # Get relevant documents from MongoDB
    relevant_docs = get_logs(user_message)
    #relevant_docs = get_relevant_documents(user_message)

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
            yield f"data: { json.dumps({'error': error_message}) }\n\n"
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
                        yield f"data: { json.dumps({'content': content}) }\n\n"
        # Append assistant's reply to the chat history
        chat_history.append({ "role": "assistant", "content": assistant_reply })
        # Send the final message with updated history
        yield f"data: { json.dumps({ 'done': True, 'history': chat_history }) }\n\n"

    return Response(generate(), mimetype='text/event-stream')

if __name__ == '__main__':
    app.run(debug=True, host="0.0.0.0", port=9494)

