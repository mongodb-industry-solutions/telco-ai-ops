#
# Copyright (c) 2024, 2025 MongoDB Inc.
# Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
#

from flask import Flask, render_template, request, jsonify, Response
from openai import OpenAI
import os, re, datetime, requests, json, pymongo, tiktoken

app = Flask(__name__)

XAI_API_KEY = os.getenv('XAI_API_KEY')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')

# The telemetrics data - adapt this to your needs!
MCONN = os.getenv('MONGODB_TELCO_CHAT') # the MongoDB connection string
MBASE = os.getenv('MONGODB_TELCO_CHAT_DATABASE') # the database to be used
MCOLL = os.getenv('MONGODB_TELCO_CHAT_COLLECTION') # ...and the collection

access_log_collection = pymongo.MongoClient(MCONN)[MBASE][MCOLL]

#ai = OpenAI(api_key = XAI_API_KEY, base_url = "https://api.x.ai/v1")
ai = OpenAI()

MAX_TOKENS = 8192
RESERVED_TOKENS = 1000

encoding = tiktoken.encoding_for_model('gpt-4o')

latest_pipeline = []


def process_pipeline(pipeline_str):
    from datetime import datetime
    from bson import ObjectId

    def ISODate(date_str):
        return datetime.fromisoformat(date_str.replace('Z', '+00:00'))

    local_scope = {'ISODate': ISODate, 'ObjectId': ObjectId}
    return eval(pipeline_str, {"__builtins__": None}, local_scope)


def gen(text):
    prompt = f"""

    I need you as a MongoDB Aggregation Pipeline Builder. You
    translate my natural language queries into appropriate pipelines
    that return MongoDB data and lead to efficient results in a RAG
    architecture. The underlying MongoDB data schema is as follows:

    {{
      _id: ObjectId('668551649101ed2d266dd505'),
      timestamp: ISODate('2024-07-03T13:25:56.391Z'),
      path: '/backstage',
      ip: '104.30.134.186',
      city: 'Copenhagen',
      country: 'DK'
    }}

    Please always return only the pipeline in Python syntax as a list,
    without any additional formatting or code blocks such as
    JavaScript or Python.

    Important: The results when executing the generated pipeline shall
    be sufficiently expressive to serve as contextual
    input. Aggregations, in particular, should include a clear title
    or prefix that precisely describes what is being aggregated. Add
    appropriate steps like $addFields or $project in the pipeline to,
    for instance, create a field aggregation_title with a description
    of the aggregation. Transform numbers and digits into strings, for
    example, when calculating $dayOfWeek and $dayOfMonth.

    If data for specific date, month, day, or weekday is requested,
    assume that we have the year 2025 and include this in the query
    generation if no other year was mentioned.

    To filter dates, always use true ISODates. And work with $expr,
    $and where you see fit.

    Important: Never include stages in the pipeline that perform write
    operations on the database, such as $merge. The calculations of
    the pipeline must always be directly output.

    To repeat: Only provide the Python list, without any additional
    prefixes or suffixes.

    {text}
    """

    try:
        response = ai.chat.completions.create(
            #model = "grok-3-latest",
            model = "gpt-4.1",
            messages = [
                { "role" : "system", "content" : "You are a MongoDB query creation assistant." },
                { "role" : "user", "content" : prompt }
            ]
            #max_tokens = 2000,
            #n = 1,
            #temperature = 0.7
        )
        pipeline = response.choices[0].message.content.strip()
        return pipeline

    except Exception as e:
        print(f"An error occurred: {e}")
        return None


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


def get_logs(user_input):
    try:
        pipeline = gen(user_input)
        if pipeline:
            try:
                # preserve latest pipeline
                latest_pipeline.clear()
                latest_pipeline.append(pipeline)
                # and execute it
                pipeline_converted = process_pipeline(pipeline)
                context = str(list(access_log_collection.aggregate(pipeline_converted)))
                if len(context) < 256000:
                    return pipeline, context
                else:
                    return pipeline, "No useful context could be calculated, as your question was too generic / wide."
            except Exception as e:
                print(e)
                return "", ""
        else:
            return "", ""

    except Exception as e:
        print("get_logs(): " + str(e))
        exit(1)


@app.route('/')
def home():
    return render_template('index.html')


def shorten(text):
    lines = text.split('\n')
    if len(lines) > 55:
        shortened = lines[:55]
        return '\n'.join(shortened) + '\n\n... skipped lines ...'
    else:
        return text


@app.route("/latest-pipeline")
def get_latest_pipeline():
    return Response(shorten(latest_pipeline[0]), mimetype='text/plain')


@app.route('/chat', methods=['POST'])
def chat():
    user_message = request.json.get('message')
    chat_history = request.json.get('history', [])

    # Define the system prompt
    system_prompt = { "role": "system", "content":
                      """
                      You are a telco network AI-OPS chatbot.
                      I can communicate with you about the health of the network
                      and ask about current stats. You will find latest aggregated
                      data in your context prompt. Please use that to provide
                      meaingful answers, including projections into the future where
                      appropriate or asked for.
                      """
                     }

    # Ensure the system prompt is at the beginning of the chat history
    if not chat_history or chat_history[0].get('role') != 'system':
        chat_history.insert(0, system_prompt)

    # Append the user's message to the chat history
    chat_history.append({ "role": "user", "content": user_message })

    # Get relevant documents from MongoDB
    mongo_query, returned_logs = get_logs(user_message)
    #relevant_docs = get_relevant_documents(user_message)

    # Incorporate the context into the conversation
    if len(returned_logs) > 10:
        context = mongo_query + "\n\n" + returned_logs
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
    app.run(debug=False, use_reloader=False, host="127.0.0.1", port=9494)

