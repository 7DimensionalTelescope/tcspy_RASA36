


#%%%
from slack_sdk import WebClient
import html
import http.client
import os
import re
import time
#%%
class SlackConnector:

    # Seconds before the first post attempt still searched for a delivered
    # message, to absorb clock differences between this host and Slack.
    DELIVERY_LOOKUP_MARGIN = 30.0
    # A post can appear in Slack shortly after its response was lost.
    DELIVERY_LOOKUP_ATTEMPTS = 3
    DELIVERY_LOOKUP_DELAY = 2.0

    def __init__(self,
                 token_path: str = f'{os.path.expanduser("~")}/configuration/keys/slack_token.txt',
                 default_channel_id: str = None):
        token = open(token_path, 'r').read()
        self.client = WebClient(token=token)
        self.channel_id = default_channel_id
        self.channel_name = self._get_channel_name(default_channel_id) if default_channel_id else None

    def __repr__(self):
        channel_info = f"Channel: {self.channel_name or 'No channel selected'}"
        return f"<SlackConnector({channel_info})>"

    def _call_with_retry(self, func, *args, max_attempts: int = 3, **kwargs):
        """
        Call a Slack API function, retrying on transient network errors
        (truncated reads and read timeouts occasionally occur on this
        network, due to an SSL-inspecting firewall in the path).
        Only for calls that are safe to repeat; posts use _post_with_retry.
        """
        for attempt in range(1, max_attempts + 1):
            try:
                return func(*args, **kwargs)
            except (http.client.IncompleteRead, TimeoutError) as e:
                if attempt == max_attempts:
                    raise
                print(f'{func.__name__} failed ({e}). Retrying ({attempt}/{max_attempts - 1})...')

    def _post_with_retry(self, func, is_match, channel_id, thread_ts = None, max_attempts: int = 3, **kwargs):
        """
        Call a Slack API function that creates content, without duplicating it.

        A lost response does not mean the content was not created, so Slack is
        searched for it before each repeat. Returns the API response, or the
        delivered message (which also has 'ts') when the response was lost. If
        Slack cannot be searched, the call is repeated anyway: a duplicate is
        acceptable, a missing message is not.
        """
        if thread_ts:
            kwargs['thread_ts'] = thread_ts
        oldest = time.time() - self.DELIVERY_LOOKUP_MARGIN
        for attempt in range(1, max_attempts + 1):
            try:
                return func(channel=channel_id, **kwargs)
            except (http.client.IncompleteRead, TimeoutError) as e:
                try:
                    delivered = self._find_delivered(is_match, channel_id, thread_ts, oldest)
                except Exception as lookup_error:
                    print(f'WARNING: {func.__name__} failed ({e}) and delivery could not be checked ({lookup_error}). A repeat may duplicate it.')
                    delivered = None
                if delivered is not None:
                    print(f'{func.__name__} response lost ({e}), but it was delivered.')
                    return delivered
                if attempt == max_attempts:
                    raise
                print(f'{func.__name__} failed ({e}). Retrying ({attempt}/{max_attempts - 1})...')

    def _find_delivered(self, is_match, channel_id, thread_ts, oldest):
        """
        Return the message posted after oldest that satisfies is_match, or None.
        """
        for _ in range(self.DELIVERY_LOOKUP_ATTEMPTS):
            time.sleep(self.DELIVERY_LOOKUP_DELAY)
            if thread_ts:
                result = self._call_with_retry(
                    self.client.conversations_replies,
                    channel=channel_id, ts=str(thread_ts), oldest=f'{oldest:.6f}', limit=200)
            else:
                result = self._call_with_retry(
                    self.client.conversations_history,
                    channel=channel_id, oldest=f'{oldest:.6f}', limit=200)
            for message in result['messages']:
                # conversations_replies can include the parent message itself.
                if message['ts'] != str(thread_ts) and float(message['ts']) >= oldest and is_match(message):
                    return message
        return None

    @classmethod
    def _content_key(cls, text = None, blocks = None):
        """
        Reduce message content to letters and digits for comparison, because
        Slack rewrites entities, links and emoji in the text it returns.
        """
        def _texts(node):
            if isinstance(node, dict):
                for key in sorted(node):
                    if key == 'text' and isinstance(node[key], str):
                        yield node[key]
                    else:
                        yield from _texts(node[key])
            elif isinstance(node, (list, tuple)):
                for item in node:
                    yield from _texts(item)

        content = ' '.join(_texts(blocks)) if blocks else (text or '')
        content = re.sub(r'<([^<>|]*)\|([^<>]*)>', r'\2', content)
        content = re.sub(r':[a-z][a-z0-9_+-]*:', '', html.unescape(content))
        return ''.join(c for c in content if c.isalnum())

    def _post_content(self, channel_id, text, blocks, thread_ts = None):
        """
        Post a message to a channel, or to a thread when thread_ts is given.
        """
        wanted = self._content_key(text, blocks)

        def is_match(message):
            # Text-only posts come back with generated blocks, so compare like with like.
            found = self._content_key(blocks = message.get('blocks')) if blocks else self._content_key(message.get('text'))
            return bool(wanted) and found == wanted

        return self._post_with_retry(
            self.client.chat_postMessage, is_match, channel_id, thread_ts,
            text=text, blocks=blocks)

    def _get_channel_name(self, channel_id):
        """
        Retrieve the channel name using the channel ID.
        """
        try:
            result = self._call_with_retry(self.client.conversations_info, channel=channel_id)
            return result['channel']['name']
        except Exception as e:
            print(f"Failed to fetch channel name for ID {channel_id}: {e}")
            return "Unknown Channel"

    def get_channel_id(self, channel_name):
        """
        Get channel ID based on the channel name.
        """
        private_channels = self._call_with_retry(self.client.conversations_list, types="private_channel").data['channels']
        public_channels = self._call_with_retry(self.client.conversations_list, types="public_channel").data['channels']
        channels = private_channels + public_channels
        channel = list(filter(lambda c: c["name"] == channel_name, channels))
        
        if len(channel) == 0:
            raise ValueError(f"Channel {channel_name} not found")
        else:
            return channel[0]["id"]
    
    def select_channel(self, channel_name):
        """
        Select a channel based on the channel name.
        """
        self.channel_id = self.get_channel_id(channel_name)
        self.channel_name = channel_name

    def get_message_ts(self, match_string):
        """
        Retrieve a message timestamp in the selected Slack channel.
        """
        if self.channel_id is None:
            raise ValueError("Channel is not selected")
        result = self._call_with_retry(self.client.conversations_history, channel=self.channel_id)
        messages = result.data['messages']
        matched_messages = list(filter(lambda m: match_string in m["text"], messages))
        if len(matched_messages) == 0:
            print(f"Message {match_string} not found")   
            return None
        message_ts = matched_messages[0]["ts"]
        return message_ts

    def get_messages_since(self, oldest_ts, channel = None, limit = 100):
        """
        Retrieve top-level messages posted after oldest_ts, newest first.

        Thread replies are not returned by conversations_history unless they
        were also broadcast to the channel, so those are filtered out here to
        keep one entry per conversation.
        """
        channel_id = channel or self.channel_id
        if channel_id is None:
            raise ValueError("Channel is not selected")
        result = self._call_with_retry(
            self.client.conversations_history,
            channel=channel_id,
            oldest=str(oldest_ts),
            limit=limit
        )
        messages = result.data['messages']
        return [m for m in messages if m.get('thread_ts', m.get('ts')) == m.get('ts')]

    def post_thread_message(self, message_ts, text = None, blocks = None, channel = None):
        """
        Post a message to a thread in the selected Slack channel.
        """
        channel_id = channel or self.channel_id
        if channel_id is None:
            raise ValueError("Channel is not selected")
        result = self._post_content(channel_id, text, blocks, thread_ts = message_ts)
        print(f'Thread message posted: text = {text}, blocks = {blocks}')
        return result

    def upload_file_to_thread(self, file_path: str, thread_ts: str, title: str = None):
        """Upload a file as a reply to an existing Slack thread."""
        if self.channel_id is None:
            raise ValueError("Channel is not selected")
        title = title or os.path.basename(file_path)

        def is_match(message):
            return any(f.get('title') == title for f in message.get('files') or [])

        self._post_with_retry(
            self.client.files_upload_v2, is_match, self.channel_id, thread_ts,
            file=file_path, title=title)

    def post_message(self, text = None, blocks = None):
        """
        Post a message to the selected Slack channel.
        """
        if self.channel_id is None:
            raise ValueError("Channel is not selected")
        result = self._post_content(self.channel_id, text, blocks)
        print(f'Message posted: text = {text}, blocks = {blocks}')
        return result
