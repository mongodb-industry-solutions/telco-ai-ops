//
// Copyright (c) 2024, 2025 MongoDB Inc.
// Author: Benjamin Lorenz <benjamin.lorenz@mongodb.com>
//

let chatHistory = [];

function updatePipelineSidebar() {
    fetch('/latest-pipeline')
        .then(res => res.text())
        .then(text => {
            const formatted = text
                  .replace(/&/g, '&amp;')
                  .replace(/</g, '&lt;')
                  .replace(/>/g, '&gt;')
                  .replace(/\n/g, '<br>')
                  .replace(/  /g, '&nbsp;&nbsp;');

            document.getElementById('sidebar').innerHTML = `
              <h2 style="font-size: 1.25rem; color: #444;">Aggregation Pipeline</h2>
              <div style="font-family: monospace; font-size: 0.6rem; line-height: 1.3;">${formatted}</div>
            `;
        });
}

function handleChatFormSubmit(e, customMessage = null) {
    if (e) e.preventDefault();
    let message = customMessage || $('#message-input').val();
    $('#message-input').val('');

    // Display user's message
    $('#chat-box').append(`
            <div class="message user">
                <div class="content">${message}</div>
            </div>
        `);
    $('#chat-box').scrollTop($('#chat-box')[0].scrollHeight);

    // Update chat history
    chatHistory.push({"role": "user", "content": message});

    // Create a placeholder for the assistant's message
    let assistantMessageDiv = $(`
            <div class="message assistant">
                <div class="content"></div>
            </div>
        `);
    $('#chat-box').append(assistantMessageDiv);
    $('#chat-box').scrollTop($('#chat-box')[0].scrollHeight);

    let assistantMessageContent = assistantMessageDiv.find('.content');

    // Use Fetch API to send POST request and handle streaming response
    fetch('/chat', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            message: message,
            history: chatHistory
        })
    })
    .then(response => {
        if (!response.ok) {
            throw new Error('Network response was not ok.');
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        function read() {
            reader.read().then(({ done, value }) => {
                if (done) {
                    return;
                }
                buffer += decoder.decode(value, { stream: true });
                let lines = buffer.split('\n\n');
                buffer = lines.pop(); // Save incomplete line

                for (let line of lines) {
                    if (line.startsWith('data: ')) {
                        let data = JSON.parse(line.slice(6));
                        if (data.error) {
                            alert(data.error);
                            return;
                        }
                        if (data.content) {
                            assistantMessageContent.append(data.content);
                            $('#chat-box').scrollTop($('#chat-box')[0].scrollHeight);
                        }
                        if (data.done) {
				            parsed = marked.parse(assistantMessageContent[0].innerHTML);
                            assistantMessageContent[0].innerHTML = parsed;
                            $('#chat-box').scrollTop($('#chat-box')[0].scrollHeight);
                            updatePipelineSidebar();
                            chatHistory = data.history;
                        }
                    }
                }
                read();

            }).catch(error => {
                console.error('Error reading stream:', error);
            });
        }
        read();
        })
        .catch(error => {
            console.error('Fetch error:', error);
            alert('An error occurred while communicating with the server.');
        });
};

$(document).ready(function() {
    $('#chat-form').on('submit', handleChatFormSubmit);

    $('.chat-shortcut').on('click', function(e) {
        let message = $(this).data('message');
        handleChatFormSubmit(e, message);
    });

});
