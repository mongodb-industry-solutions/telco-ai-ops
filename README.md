# telco.chat - GenAI-Ops Demo
This demo showcases an AI-powered introspection into network logs. Context for the LLM is build by means of executing MongoDB
aggregation pipelines, which in turn are defined by the same LLM, in the first stage of processing each question in the chat.

## Installation

#### Set environment variables to access your OpenAI token and your MongoDB Atlas cluster

```
export OPENAI_API_KEY='<key>'

export MONGODB_TELCO_CHAT='<connection string>'
export MONGODB_TELCO_CHAT_DATABASE='<database name>'
export MONGODB_TELCO_CHAT_COLLECTION='<collection name>'
```
The network (webserver) logs that I am using have the following format.
This is currently hardcoded into the demo:

```
{
      _id: ObjectId('668551649101ed2d266dd505'),
      timestamp: ISODate('2024-07-03T13:25:56.391Z'),
      path: '/backstage',
      ip: '104.30.134.186',
      city: 'Copenhagen',
      country: 'DK'
}
 ```
   
#### Install Python 3.11

```
brew install python@3.11
```
#### Set the path in your .zshrc

```
export PATH="$(brew --prefix)/opt/python@3.11/libexec/bin:$PATH"
```

#### Create and activate a Python virtual environment

```
python3 -m venv <dir>
cd <dir>; source ./bin/activate
```

#### Install Python packages

```
pip install -r telco-ai-ops/requirements.txt
```

#### Start the application:

```
cd telco-ai-ops
python app.py
```

If all goes well, you can access the app from your browser at localhost:9494.

## Acknowledgements

I want to thank Genevieve Broadhead and Boris Bialek for giving me the opportunity to build this demo - it is so much fun!

[Benjamin Lorenz](https://www.linkedin.com/in/benjaminlorenz/)

