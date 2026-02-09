import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
import random
import threading
import time

app = Flask(__name__, static_folder='static')
app.config['SECRET_KEY'] = 'familytruths_secret'
socketio = SocketIO(app, async_mode='eventlet')

# --- DATA STRUCTURES ---

class Question:
    def __init__(self, text, answer, house_lies=None):
        self.text = text
        self.answer = answer
        self.house_lies = house_lies if house_lies else []

    def to_dict(self):
        return {
            'text': self.text,
            'answer': self.answer,
            'house_lies': self.house_lies
        }

class GameState:
    LOBBY = 'LOBBY'
    BLUFF_INPUT = 'BLUFF_INPUT'
    VOTING = 'VOTING'
    REVEAL = 'REVEAL'
    SCOREBOARD = 'SCOREBOARD'

    def __init__(self):
        self.state = self.LOBBY
        self.players = {}  # session_id -> {name, score, connected}
        self.questions = []
        self.current_question_index = 0
        
        # Round Data
        self.submissions = {} # session_id -> lie_text
        self.house_lies_active = [] # List of house lies used for this round
        self.votes = {} # session_id -> voted_text
        
        # Timer
        self.timer_thread = None
        self.time_left = 0

    def add_player(self, sid, name):
        self.players[sid] = {'name': name, 'score': 0, 'connected': True}

    def remove_player(self, sid):
        if sid in self.players:
            self.players[sid]['connected'] = False

    def next_round(self):
        self.current_question_index += 1
        if self.current_question_index >= len(self.questions):
            return False # Game Over or loop?
        return True

    def reset_round_data(self):
        self.submissions = {}
        self.votes = {}
        self.house_lies_active = []

# Global Game Instance
game = GameState()

# --- HELPER FUNCTIONS ---

def broadcast_state():
    # Send different data to Host vs Players if needed, 
    # but for simplicity we send a generic state update and clients handle their view.
    # However, we must NOT reveal the Truth or other players' lies during INPUT/VOTING phases to players.
    
    common_data = {
        'state': game.state,
        'time_left': game.time_left
    }
    
    # HOST DATA
    host_data = common_data.copy()
    host_data['players'] = list(game.players.values())
    if game.state == GameState.LOBBY:
        host_data['room_code'] = "FAMILY"
        
    elif game.state == GameState.BLUFF_INPUT:
        q = game.questions[game.current_question_index]
        host_data['question'] = q.text
        # Show who has submitted
        host_data['submitted_players'] = [game.players[sid]['name'] for sid in game.submissions]

    elif game.state == GameState.VOTING:
        q = game.questions[game.current_question_index]
        host_data['question'] = q.text
        # Combine Truth + Player Lies + House Lies
        options = [q.answer] + list(game.submissions.values()) + game.house_lies_active
        # Shuffle is handled once when entering state, but for statelessness we might re-shuffle or store order. 
        # Ideally store the options order in game state to keep it consistent.
        host_data['options'] = game.current_options # Need to store this

    elif game.state == GameState.REVEAL:
         host_data['reveal_sequence'] = game.reveal_sequence
         host_data['question'] = game.questions[game.current_question_index].text
         host_data['truth'] = game.questions[game.current_question_index].answer

    elif game.state == GameState.SCOREBOARD:
        host_data['leaderboard'] = sorted(list(game.players.values()), key=lambda x: x['score'], reverse=True)

    socketio.emit('state_update', host_data)

def start_timer(duration, next_callback):
    game.time_left = duration
    def timer_loop():
        while game.time_left > 0:
            socketio.sleep(1)
            game.time_left -= 1
            socketio.emit('timer_update', {'time_left': game.time_left})
        
        # Time's up!
        with app.app_context():
            next_callback()
            
    if game.timer_thread:
        game.timer_thread.kill() # specific to eventlet/gevent
    game.timer_thread = socketio.start_background_task(timer_loop)

def auto_advance_input():
    # If time runs out, submit default lies for those who missed
    for sid, p in game.players.items():
        if sid not in game.submissions and p['connected']:
            game.submissions[sid] = f"{p['name']} fell asleep"
    transition_to_voting()

def auto_advance_voting():
    transition_to_reveal()

def transition_to_voting():
    game.state = GameState.VOTING
    q = game.questions[game.current_question_index]
    
    # Prepare House Lies
    # (Select random ones if too many, or all)
    game.house_lies_active = q.house_lies
    
    # Prepare Options
    options = [{'text': q.answer, 'type': 'TRUTH', 'author': 'HOUSE'}]
    for sid, lie in game.submissions.items():
        options.append({'text': lie, 'type': 'LIE', 'author': game.players[sid]['name'], 'sid': sid})
    for lie in game.house_lies_active:
        options.append({'text': lie, 'type': 'HOUSE_LIE', 'author': 'HOUSE'})
        
    random.shuffle(options)
    game.current_options = options
    
    broadcast_state()
    start_timer(30, auto_advance_voting)

def transition_to_reveal():
    game.state = GameState.REVEAL
    
    # Calculate Scores
    q = game.questions[game.current_question_index]
    reveal_events = []
    
    # 1. Reveal Lies
    for option in game.current_options:
        text = option['text']
        if option['type'] == 'TRUTH':
            continue # doing truth last
        
        # Who voted for this?
        voters = []
        for vid, vote_text in game.votes.items():
            if vote_text == text:
                voters.append(game.players[vid]['name'])
                # Award points to author if it's a player lie
                if option['type'] == 'LIE':
                    author_sid = option['sid']
                    # Don't award if voting for own lie (should be blocked frontend, but safety check)
                    if vid != author_sid:
                        game.players[author_sid]['score'] += 500
        
        reveal_events.append({
            'type': 'LIE',
            'text': text,
            'author': option['author'],
            'voters': voters
        })

    # 2. Reveal Truth
    truth_text = q.answer
    truth_voters = []
    for vid, vote_text in game.votes.items():
        if vote_text == truth_text:
            truth_voters.append(game.players[vid]['name'])
            game.players[vid]['score'] += 1000
    
    reveal_events.append({
        'type': 'TRUTH',
        'text': truth_text,
        'author': 'THE TRUTH',
        'voters': truth_voters
    })
    
    game.reveal_sequence = reveal_events
    broadcast_state()

# --- ROUTES ---

@app.route('/')
def index():
    return render_template('host.html')

@app.route('/player')
def player():
    return render_template('player.html')

# --- SOCKET EVENTS ---

@socketio.on('connect')
def connect():
    emit('connected', {'sid': request.sid})

@socketio.on('host_login')
def host_login():
     # Host doesn't need to do much
    emit('state_update', {'state': game.state, 'questions': [q.to_dict() for q in game.questions]})

@socketio.on('join_game')
def join_game(data):
    name = data.get('name')
    game.add_player(request.sid, name)
    # Broadcast to host
    broadcast_state()
    emit('joined_success', {'name': name})

@socketio.on('add_question')
def add_question(data):
    print(f"Received question: {data}")
    q = data.get('question')
    a = data.get('answer')
    hl = data.get('house_lies', [])
    # filter empty
    hl = [l for l in hl if l.strip()]
    
    new_q = Question(q, a, hl)
    game.questions.append(new_q)
    print(f"Question added. Total: {len(game.questions)}")
    emit('question_added', new_q.to_dict(), broadcast=True)

@socketio.on('start_game')
def start_game_event():
    if not game.questions:
        return 
    game.state = GameState.BLUFF_INPUT
    game.current_question_index = 0
    game.reset_round_data()
    broadcast_state()
    start_timer(60, auto_advance_input)

@socketio.on('submit_lie')
def submit_lie(data):
    lie = data.get('lie')
    # Fuzzy match check against truth
    current_q = game.questions[game.current_question_index]
    if lie.lower().strip() == current_q.answer.lower().strip():
        emit('error', {'message': "Too close to the Truth! Try again."})
        return
        
    game.submissions[request.sid] = lie
    emit('submitted_success')
    broadcast_state() # Updates host "Bob is Ready"
    
    # Check if all submitted
    active_players = [p for sid, p in game.players.items() if p['connected']]
    if len(game.submissions) >= len(active_players):
        # All in, cut timer short
        transition_to_voting()

@socketio.on('submit_vote')
def submit_vote(data):
    vote = data.get('vote')
    game.votes[request.sid] = vote
    emit('voted_success')
    
    active_players = [p for sid, p in game.players.items() if p['connected']]
    if len(game.votes) >= len(active_players):
         transition_to_reveal()

@socketio.on('next_round')
def next_round_event():
    if game.next_round():
        game.state = GameState.BLUFF_INPUT
        game.reset_round_data()
        broadcast_state()
        start_timer(60, auto_advance_input)
    else:
        game.state = GameState.SCOREBOARD
        broadcast_state()

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0')
