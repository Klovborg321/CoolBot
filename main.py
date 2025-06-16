import requests
import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import random
import json
import asyncio
from dotenv import load_dotenv
import asyncio
from functools import partial
from discord import app_commands, Interaction, SelectOption, ui, Embed
from supabase import create_client, Client
import os

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = None

async def run_db(fn):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fn)

def setup_supabase():
    global supabase
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

setup_supabase()  # ← runs immediately when script loads!

# ✅ Discord intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


IS_TEST_MODE = os.getenv("TEST_MODE", "1") == "1"
start_buttons = {}  # (channel_id, game_type) => Message

# Globals
games = {}

pending_games = {
    "singles": None,
    "doubles": None,
    "triples": None,
    "tournament": None
}

players_data = "players.json"


WORDS = ["alpha", "bravo", "delta", "foxtrot", "gamma"]

default_template = {
    "rank": 1000,
    "trophies": 0,
    "credits": 1000,
    "wins": 0,
    "losses": 0,
    "draws": 0,
    "games_played": 0,
    "current_streak": 0,
    "best_streak": 0
}

# Helpers

async def dm_all_online(guild: discord.Guild, message: str):
    """DM all online members in the given guild with a custom message."""
    # Make sure your bot has `members` intent enabled!
    if not guild.me.guild_permissions.administrator:
        print("⚠️ Bot may not have permission to read member list.")
    
    sent = 0
    failed = 0

    for member in guild.members:
        # Skip bots, offline, and the bot itself
        if member.bot or member.status == discord.Status.offline or member == guild.me:
            continue

        try:
            await member.send(message)
            sent += 1
        except discord.Forbidden:
            # User DMs closed or bot blocked
            failed += 1
        except Exception as e:
            print(f"Error sending to {member}: {e}")
            failed += 1

    print(f"✅ Done. Sent to {sent} users, failed for {failed}.")


# ✅ Save a pending game (async)
async def save_pending_game(game_type, players, channel_id):
    await run_db(lambda: supabase.table("pending_games").upsert({
        "game_type": game_type,
        "players": players,
        "channel_id": channel_id
    }).execute())

# ✅ Clear a pending game (async)
async def clear_pending_game(game_type):
    await run_db(lambda: supabase.table("pending_games").delete().eq("game_type", game_type).execute())

# ✅ Load all pending games (async)
async def load_pending_games():
    response = await run_db(lambda: supabase.table("pending_games").select("*").execute())
    return response.data

async def deduct_credits_atomic(user_id: int, amount: int) -> bool:
    res = await run_db(
        lambda: supabase.rpc("deduct_credits_atomic", {
            "user_id": user_id,  # ✅ pass as INT
            "amount": amount
        }).execute()
    )

    # 📌 Use `getattr` fallback to avoid AttributeError
    if getattr(res, "status_code", 200) != 200:
        print(f"[Supabase RPC Error] Status: {getattr(res, 'status_code', '??')} Data: {res.data}")
        return False

    return bool(res.data)


async def add_credits_internal(user_id: int, amount: int):
    # ✅ Fetch current player
    user = await get_player(user_id)
    current_credits = user.get("credits", 0)

    # ✅ Compute new balance
    new_credits = current_credits + amount

    # ✅ Update back to Supabase
    await run_db(lambda: supabase
        .table("players")
        .update({"credits": new_credits})
        .eq("id", str(user_id))
        .execute()
    )

    return new_credits


async def save_player(user_id: int, player_data: dict):
    player_data["id"] = str(user_id)
    await run_db(lambda: supabase
        .table("players")
        .upsert(player_data)
        .execute())


async def handle_bet(interaction, user_id, choice, amount, odds, game_id):
    # ✅ Try atomic deduction
    success = await deduct_credits_atomic(user_id, amount)
    if not success:
        await interaction.response.send_message("❌ Not enough credits.", ephemeral=True)
        return

    # ✅ Log the bet
    payout = int(amount / odds) if odds > 0 else amount

    await run_db(lambda: supabase.table("bets").insert({
        "player_id": str(user_id),
        "game_id": game_id,
        "choice": choice,
        "amount": amount,
        "payout": payout,
        "won": None
    }).execute())

    await interaction.response.send_message(
        f"✅ Bet of {amount} placed on {choice}. Potential payout: {payout}",
        ephemeral=True
    )



async def get_complete_user_data(user_id):
    res = await run_db(lambda: supabase.table("players").select("*").eq("id", str(user_id)).single().execute())

    if res.data is None:
        # Not found → insert defaults
        defaults = default_template.copy()
        defaults["id"] = str(user_id)
        await run_db(lambda: supabase.table("players").insert(defaults).execute())
        return defaults

    return res.data


async def update_user_stat(user_id, key, value, mode="set"):
    res = await run_db(lambda: supabase.table("players").select("*").eq("id", str(user_id)).single().execute())

    if res.data is None:
        # Player missing, create fresh
        data = default_template.copy()
        data["id"] = str(user_id)
    else:
        data = res.data

    if mode == "set":
        data[key] = value
    elif mode == "add":
        data[key] = data.get(key, 0) + value

    await save_player(user_id, data)




# Load ALL players as a dict
# ✅ Safe get_player: always upsert if not exists
async def get_player(user_id: int) -> dict:
    # Safely select
    res = await run_db(lambda: supabase.table("players").select("*").eq("id", str(user_id)).execute())

    if res.data is None:
        # No row found → create one
        new_data = default_template.copy()
        new_data["id"] = str(user_id)
        await run_db(lambda: supabase.table("players").insert(new_data).execute())
        return new_data

    return res.data[0]


def calculate_elo(elo1, elo2, result):
    expected = 1 / (1 + 10 ** ((elo2 - elo1) / 400))
    return elo1 + 32 * (result - expected)

def player_display(user_id, data):
    player = data.get(str(user_id), {"rank": 1000, "trophies": 0})
    return f"<@{user_id}> | Rank: {player['rank']} | Trophies: {player['trophies']}"

async def start_new_game_button(channel, game_type, max_players=None):
    key = (channel.id, game_type)
    old = start_buttons.get(key)

    if old:
        try:
            await old.delete()
        except discord.NotFound:
            pass

    if game_type == "tournament":
        # For tournament, we create the Start Tournament button
        view = TournamentStartButtonView()
        msg = await channel.send("🎮 Click to start a new tournament:", view=view)
    else:
        # For other game types, create the GameJoinView with max_players passed
        view = GameJoinView(game_type, max_players)
        msg = await channel.send(f"🎮 Start a new {game_type} game:", view=view)

    start_buttons[key] = msg
    return msg  # ✅ return it!



async def show_betting_phase(self):
    self.clear_items()
    self.add_item(BettingButtonDropdown(self))
    await self.update_message()

    await asyncio.sleep(120)
    self.betting_closed = True  # ✅ Mark it closed
    self.clear_items()
    await self.update_message()  # ✅ This will now show "Betting is closed" in footer

async def update_message(self):
    if self.message:
        embed = await self.build_embed(self.message.guild)
        await self.message.edit(embed=embed, view=self)

class PlayerManager:
    def __init__(self):
        self.active_players = set()

    def is_active(self, user_id):
        return user_id in self.active_players

    def activate(self, user_id):
        self.active_players.add(user_id)

    def deactivate(self, user_id):
        self.active_players.discard(user_id)

    def deactivate_many(self, user_ids):
        for uid in user_ids:
            self.deactivate(uid)

    def clear(self):
        self.active_players.clear()

player_manager = PlayerManager()

class RoomNameGenerator:
    def __init__(self):
        self.word_cache = []
        self.used_words = set()
        self.fetching = False

    async def fetch_five_letter_words(self):
        if self.fetching:
            return  # prevent multiple calls
        self.fetching = True
        try:
            response = requests.get(
                "https://api.datamuse.com/words", params={"sp": "?????", "max": 1000}
            )
            words = [w["word"].lower() for w in response.json() if w["word"].isalpha()]
            self.word_cache = [w for w in words if w not in self.used_words]
        except Exception as e:
            print(f"[RoomNameGenerator] Error: {e}")
        finally:
            self.fetching = False

    async def get_unique_word(self):
        if not self.word_cache:
            await self.fetch_five_letter_words()
        if not self.word_cache:
            return "RoomX"
        word = random.choice(self.word_cache)
        self.word_cache.remove(word)
        self.used_words.add(word)
        return word.capitalize()

# ✅ use `await room_name_generator.get_unique_word()` in your flow



# ✅ Correct: instantiate it OUTSIDE the class block
room_name_generator = RoomNameGenerator()


class GameJoinView(discord.ui.View):
    def __init__(self, game_type, max_players):
        super().__init__(timeout=None)
        self.game_type = game_type
        self.max_players = max_players

    @discord.ui.button(label="Start new game", style=discord.ButtonStyle.primary)
    async def start_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        # ✅ Block duplicate games of same type
        if pending_games[self.game_type]:
            await interaction.response.send_message(
                "A game of this type is already pending.", ephemeral=True)
            return

        # ✅ Block ANY other active game (cross-lobby)
        if player_manager.is_active(interaction.user.id):
            await interaction.response.send_message(
                "🚫 You are already in another game or have not voted yet.", ephemeral=True)
            return

        # ✅ OK! Activate and start
        player_manager.activate(interaction.user.id)

        # Pass max_players to the GameView initialization
        view = GameView(self.game_type, interaction.user.id, self.max_players)
        embed = await view.build_embed(interaction.guild)
        view.message = await interaction.channel.send(embed=embed, view=view)
        pending_games[self.game_type] = view

        try:
            await interaction.message.delete()
        except discord.NotFound:
            pass

        await interaction.response.send_message("✅ Game started!", ephemeral=True)



class LeaveGameButton(discord.ui.Button):
    def __init__(self, game_view):
        super().__init__(label="Leave Game", style=discord.ButtonStyle.danger)
        self.game_view = game_view

    async def callback(self, interaction: discord.Interaction):
        user_id = interaction.user.id

        if user_id not in self.game_view.players:
            await interaction.response.send_message("You are not in this game.", ephemeral=True)
            return

        self.game_view.players.remove(user_id)
        player_manager.deactivate(user_id)

        await self.game_view.update_message()
        await interaction.response.send_message("✅ You have left the game.", ephemeral=True)

        # ✅ Abandon only if lobby is empty
        if len(self.game_view.players) == 0:
            await self.game_view.abandon_game("❌ Game abandoned because all players left.")



class BettingButtonDropdown(discord.ui.Button):
    def __init__(self, game_view):
        super().__init__(label="Place Bet", style=discord.ButtonStyle.primary)
        self.game_view = game_view

    async def callback(self, interaction: discord.Interaction):
        # ✅ Create view and pre-build dropdown options safely:
        view = BettingDropdownView(self.game_view)
        await view.prepare()

        await interaction.response.send_message(
            "Select who you want to bet on:",
            view=view,
            ephemeral=True
        )


class GameView(discord.ui.View):
    def __init__(self, game_type, creator, max_players):
        super().__init__(timeout=None)
        self.game_type = game_type
        self.creator = creator
        self.players = [creator]
        self.max_players = max_players
        self.message = None
        self.betting_closed = False
        self.bets = []
        self.abandon_task = asyncio.create_task(self.abandon_if_not_filled())
        self.add_item(LeaveGameButton(self))
        self.on_tournament_complete = None  # ✅ callback for tournament to hook into

    async def abandon_game(self, reason):
        global pending_game
        pending_games[self.game_type] = None

        # Deactivate everyone
        for p in self.players:
            player_manager.deactivate(p)

        embed = discord.Embed(
            title="❌ Game Abandoned",
            description=reason,
            color=discord.Color.red()
        )
        await self.message.edit(embed=embed, view=None)

        await start_new_game_button(self.message.channel, self.game_type)

    async def abandon_if_not_filled(self):
        await asyncio.sleep(1000)
        if len(self.players) < self.max_players and not self.betting_closed:
            await self.abandon_game("⏰ Game timed out due to inactivity.")
            await clear_pending_game(self.game_type)

    async def build_embed(self, guild=None, winner=None):
        embed = discord.Embed(
            title=f"🎮 {self.game_type.title()} Match Lobby",
            description="Awaiting players for a new match...",
            color=discord.Color.orange() if not winner else discord.Color.dark_gray()
        )
        embed.set_author(
            name="LEAGUE OF EXTRAORDINARY MISFITS",
            icon_url="https://cdn.discordapp.com/attachments/1378860910310854666/1382601173932183695/LOGO_2.webp"
        )

        embed.timestamp = discord.utils.utcnow()  # ✅ Add timestamp

        # ✅ Get ranks from Supabase
        ranks = []
        for p in self.players:
            pdata = await get_player(p)
            ranks.append(pdata.get("rank", 1000))

        game_full = len(self.players) == self.max_players

        if self.game_type == "doubles" and game_full:
            e1 = sum(ranks[:2]) / 2
            e2 = sum(ranks[2:]) / 2
            odds_a = 1 / (1 + 10 ** ((e2 - e1) / 400))
            odds_b = 1 - odds_a
        elif self.game_type == "triples" and game_full:
            sum_exp = sum([10 ** (e / 400) for e in ranks])
            odds = [(10 ** (e / 400)) / sum_exp for e in ranks]

        player_lines = []

        if self.game_type == "doubles":
            player_lines.append("\u200b")
            label = "__**🅰️ Team A**__"
            if game_full:
                label += f" • {odds_a * 100:.1f}%"
            player_lines.append(label)

        for idx in range(self.max_players):
            if idx < len(self.players):
                user_id = self.players[idx]
                member = guild.get_member(user_id) if guild else None
                name = f"**{member.display_name}**" if member else f"**User {user_id}**"
                rank = ranks[idx]
                if self.game_type == "singles" and game_full:
                    e1, e2 = ranks
                    o1 = 1 / (1 + 10 ** ((e2 - e1) / 400))
                    player_odds = o1 if idx == 0 else 1 - o1
                    line = f"● Player {idx + 1}: {name} 🏆 ({rank}) • {player_odds * 100:.1f}%"
                elif self.game_type == "triples" and game_full:
                    line = f"● Player {idx + 1}: {name} 🏆 ({rank}) • {odds[idx] * 100:.1f}%"
                else:
                    line = f"● Player {idx + 1}: {name} 🏆 ({rank})"
            else:
                line = f"○ Player {idx + 1}: [Waiting...]"
            player_lines.append(line)

            if self.game_type == "doubles" and idx == 1:
                player_lines.append("\u200b")
                label = "__**🅱️ Team B**__"
                if game_full:
                    label += f" • {odds_b * 100:.1f}%"
                player_lines.append(label)

        embed.add_field(name="👥 Players", value="\n".join(player_lines), inline=False)

        if winner == "draw":
            embed.set_footer(text="🎮 Game has ended. Result: 🤝 Draw")
        elif isinstance(winner, int):
            member = guild.get_member(winner) if guild else None
            winner_name = member.display_name if member else f"User {winner}"
            embed.set_footer(text=f"🎮 Game has ended. Winner: {winner_name}")
        elif winner in ("Team A", "Team B"):
            embed.set_footer(text=f"🎮 Game has ended. Winner: {winner}")

        if self.bets:
            bet_lines = []
            for _, uname, amt, ch in self.bets:
                if self.game_type == "singles":
                    label = "Player 1" if ch == "1" else "Player 2"
                elif self.game_type == "doubles":
                    label = "Team A" if ch.upper() == "A" else "Team B"
                elif self.game_type == "triples":
                    label = f"Player {ch}"
                else:
                    label = ch
                bet_lines.append(f"💰 {uname} bet {amt} on {label}")
            embed.add_field(name="📊 Bets", value="\n".join(bet_lines), inline=False)

        return embed

    async def update_message(self):
        if self.message:
            embed = await self.build_embed(self.message.guild)
            to_remove = [item for item in self.children if isinstance(item, LeaveGameButton)]
            for item in to_remove:
                self.remove_item(item)
            if not self.betting_closed and len(self.players) < self.max_players:
                self.add_item(LeaveGameButton(self))
            await self.message.edit(embed=embed, view=self)

    async def get_odds(self, choice):
        ranks = []
        for p in self.players:
            pdata = await get_player(p)
            ranks.append(pdata.get("rank", 1000))

        if self.game_type == "singles":
            e1, e2 = ranks
            o1 = 1 / (1 + 10 ** ((e2 - e1) / 400))
            return o1 if choice in ("1", str(self.players[0])) else (1 - o1)
        elif self.game_type == "doubles":
            e1 = sum(ranks[:2]) / 2
            e2 = sum(ranks[2:]) / 2
            o1 = 1 / (1 + 10 ** ((e2 - e1) / 400))
            return o1 if choice.upper() == "A" else (1 - o1)
        elif self.game_type == "triples":
            exp = [10 ** (e / 400) for e in ranks]
            total = sum(exp)
            expected = [v / total for v in exp]
            if choice in ("1", str(self.players[0])):
                return expected[0]
            elif choice in ("2", str(self.players[1])):
                return expected[1]
            elif choice in ("3", str(self.players[2])):
                return expected[2]
            return 0.5

    async def add_bet(self, user_id, user_name, amount, choice):
        self.bets.append((user_id, user_name, amount, choice))
        await self.update_message()

    def get_bet_summary(self):
        if not self.bets:
            return "No bets placed yet."
        return "\n".join(f"**{uname}** bet {amt} on **{ch}**" for _, uname, amt, ch in self.bets)

    @discord.ui.button(label="Join Game", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id in self.players:
            await interaction.response.send_message("✅ You have already joined this game.", ephemeral=True)
            return
        if len(self.players) >= self.max_players:
            await interaction.response.send_message("🚫 This game is already full.", ephemeral=True)
            return
        if player_manager.is_active(interaction.user.id):
            await interaction.response.send_message("🚫 You are already in another active game.", ephemeral=True)
            return

        player_manager.activate(interaction.user.id)
        self.players.append(interaction.user.id)
        await self.update_message()
        await interaction.response.defer()

        if len(self.players) == self.max_players:
            await self.update_message()
            await self.game_full(interaction)

    async def show_betting_phase(self):
        self.clear_items()
        self.add_item(BettingButtonDropdown(self))
        await self.update_message()
        await asyncio.sleep(120)
        self.betting_closed = True
        self.clear_items()
        await self.update_message()

    async def game_full(self, interaction):
        global pending_game
        self.clear_items()
        if self.abandon_task:
            self.abandon_task.cancel()

        # ✅ This button resets the start button for new games
        await start_new_game_button(self.message.channel, self.game_type)
        pending_games[self.game_type] = None
        await save_pending_game(self.game_type, self.players, self.message.channel.id)

        # ✅ Pick a random course
        res = await run_db(lambda: supabase.table("courses").select("name", "image_url").execute())
        if res.data is None:
            course_name = "Unknown"
            course_image = ""
        else:
            chosen = random.choice(res.data)
            course_name = chosen["name"]
            course_image = chosen.get("image_url", "")

        # ✅ FIX: actually store the room name!
        room_name = await room_name_generator.get_unique_word()

        # ✅ Use the generated room name
        thread = await interaction.channel.create_thread(name=room_name)

        embed = await self.build_embed(interaction.guild)
        embed.title = f"Game Room: {room_name}"
        embed.description = f"Course: {course_name}"
        if course_image:
            embed.set_image(url=course_image)

        room_view = RoomView(
            players=self.players,
            game_type=self.game_type,
            room_name=room_name,
            lobby_message=self.message,
            lobby_embed=embed,
            game_view=self
        )
        room_view.original_embed = embed.copy()

        mentions = " ".join(f"<@{p}>" for p in self.players)
        thread_msg = await thread.send(content=f"{mentions}\nMatch started!", embed=embed, view=room_view)
        room_view.message = thread_msg

        lobby_embed = await self.build_embed(interaction.guild)
        lobby_embed.color = discord.Color.orange()
        lobby_embed.title = f"{self.game_type.title()} Match Created!"
        lobby_embed.description = f"A match has been created in thread: {thread.mention}"
        lobby_embed.add_field(name="Room Name", value=room_name)
        lobby_embed.add_field(name="Course", value=course_name)
        if course_image:
            lobby_embed.set_image(url=course_image)

        await self.message.edit(embed=lobby_embed, view=None)
        await self.show_betting_phase()


class BettingButton(discord.ui.Button):
    def __init__(self, game_view):
        super().__init__(label="Place Bet", style=discord.ButtonStyle.primary)
        self.game_view = game_view

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id in self.game_view.players:
            await interaction.response.send_message("Players cannot place bets.", ephemeral=True)
            return
        await interaction.response.send_modal(BetModal(self.game_view))

# ✅ Updated BetModal with bet_history fix
class BetModal(discord.ui.Modal, title="Place Your Bet"):
    def __init__(self, game_view):
        super().__init__()
        self.game_view = game_view

        self.bet_choice = discord.ui.TextInput(
            label="Choose (A/B/1/2)",
            placeholder="A, B, 1 or 2",
            max_length=1
        )
        self.bet_amount = discord.ui.TextInput(
            label="Bet Amount", placeholder="Enter a positive number"
        )
        self.add_item(self.bet_choice)
        self.add_item(self.bet_amount)

    async def on_submit(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        choice = self.bet_choice.value.strip().upper()

        try:
            amount = int(self.bet_amount.value.strip())
            if amount <= 0:
                raise ValueError()
        except ValueError:
            await interaction.response.send_message("❌ Invalid amount.", ephemeral=True)
            return

        # ✅ Compute odds and payout
        odds = await self.game_view.get_odds(choice)
        payout = int(amount / odds) if odds > 0 else amount

        # ✅ Atomic deduction
        success = await deduct_credits_atomic(user_id, amount)
        if not success:
            await interaction.response.send_message("❌ Not enough credits.", ephemeral=True)
            return

        # ✅ Log bet in bets table
        await run_db(lambda: supabase.table("bets").insert({
            "player_id": str(user_id),
            "game_id": self.game_view.message.id,
            "choice": choice,
            "amount": amount,
            "payout": payout,
            "won": None
        }).execute())

        # ✅ Update live bets in GameView
        await self.game_view.add_bet(user_id, interaction.user.display_name, amount, choice)

        await interaction.response.send_message(
            f"✅ Bet of **{amount}** on **{choice}** placed!\n📊 Odds: {odds * 100:.1f}% | 💰 Payout: **{payout}**",
            ephemeral=True
        )



class BetDropdown(discord.ui.Select):
    def __init__(self, game_view):
        self.game_view = game_view
        self.options_built = False  # Flag for lazy rebuild
        super().__init__(
            placeholder="Select who to bet on...",
            min_values=1,
            max_values=1,
            options=[]  # Will fill later
        )

    async def build_options(self):
        players = self.game_view.players
        game_type = self.game_view.game_type
        guild = self.game_view.message.guild if self.game_view.message else None

        options = []

        if game_type == "singles":
            ranks = []
            for p in players:
                pdata = await get_player(p)
                ranks.append(pdata.get("rank", 1000))

            e1, e2 = ranks
            p1_odds = 1 / (1 + 10 ** ((e2 - e1) / 400))
            p2_odds = 1 - p1_odds

            for i, (player_id, odds) in enumerate(zip(players, [p1_odds, p2_odds]), start=1):
                member = guild.get_member(player_id) if guild else None
                name = member.display_name if member else f"Player {i}"
                label = f"{name} ({odds * 100:.1f}%)"
                options.append(discord.SelectOption(label=label, value=str(i)))

        elif game_type == "doubles":
            ranks = []
            for p in players:
                pdata = await get_player(p)
                ranks.append(pdata.get("rank", 1000))

            e1 = sum(ranks[:2]) / 2
            e2 = sum(ranks[2:]) / 2
            a_odds = 1 / (1 + 10 ** ((e2 - e1) / 400))
            b_odds = 1 - a_odds

            options = [
                discord.SelectOption(label=f"Team A ({a_odds * 100:.1f}%)", value="A"),
                discord.SelectOption(label=f"Team B ({b_odds * 100:.1f}%)", value="B")
            ]

        elif game_type == "triples":
            ranks = []
            for p in players:
                pdata = await get_player(p)
                ranks.append(pdata.get("rank", 1000))

            exp = [10 ** (e / 400) for e in ranks]
            total = sum(exp)
            odds = [v / total for v in exp]

            for i, (player_id, o) in enumerate(zip(players, odds), start=1):
                member = guild.get_member(player_id) if guild else None
                name = member.display_name if member else f"Player {i}"
                label = f"{name} ({o * 100:.1f}%)"
                options.append(discord.SelectOption(label=label, value=str(i)))

        # ✅ Assign options safely
        self.options = options
        self.options_built = True

    async def callback(self, interaction: discord.Interaction):
        # ✅ Always safe: make sure options are built
        if not self.options_built:
            await self.build_options()

        choice = self.values[0]
        await interaction.response.send_modal(BetAmountModal(choice, self.game_view))


class RoomView(discord.ui.View):
    def __init__(self, players, game_type, room_name, lobby_message=None, lobby_embed=None, game_view=None):
        super().__init__(timeout=None)
        self.players = players
        self.game_type = game_type
        self.room_name = room_name
        self.message = None  # Thread message
        self.lobby_message = lobby_message
        self.lobby_embed = lobby_embed
        self.game_view = game_view
        self.votes = {}
        self.vote_timeout = None
        self.game_has_ended = False
        self.voting_closed = False
        self.add_item(GameEndedButton(self))

    def get_vote_options(self):
        if self.game_type == "triples":
            return self.players
        elif self.game_type == "singles":
            return self.players
        else:
            return ["Team A", "Team B"]

    async def build_lobby_end_embed(self, winner):
        embed = discord.Embed(
            title=f"{self.game_type.title()} Match",
            color=discord.Color.dark_gray()
        )

        # ✅ Get player lines from DB
        lines = []
        for p in self.players:
            pdata = await get_player(p)
            lines.append(f"<@{p}> | Rank: {pdata.get('rank', 1000)} | Trophies: {pdata.get('trophies', 0)}")

        embed.description = "\n".join(lines)
        embed.add_field(name="🎮 Status", value="Game has ended.", inline=False)

        if winner == "draw":
            embed.add_field(name="🏁 Result", value="🤝 It's a draw!", inline=False)
        elif isinstance(winner, int):
            member = self.message.guild.get_member(winner)
            name = member.display_name if member else f"User {winner}"
            embed.add_field(name="🏁 Winner", value=f"🎉 {name}", inline=False)
        elif winner in ("Team A", "Team B"):
            embed.add_field(name="🏁 Winner", value=f"🎉 {winner}", inline=False)

        return embed

    async def start_voting(self):
        if not self.game_has_ended:
            return

        self.clear_items()
        options = self.get_vote_options()
        for option in options:
            if isinstance(option, int):
                member = self.message.guild.get_member(option)
                label = member.display_name if member else f"User {option}"
            else:
                label = option
            self.add_item(VoteButton(option, self, label))

        await self.message.edit(view=self)
        self.vote_timeout = asyncio.create_task(self.end_voting_after_timeout())

    async def end_voting_after_timeout(self):
        await asyncio.sleep(600)
        await self.finalize_game()

    async def finalize_game(self):
        from collections import Counter

        vote_counts = Counter(self.votes.values())
        most_common = vote_counts.most_common()

        if not most_common:
            winner = None
        elif len(most_common) > 1 and most_common[0][1] == most_common[1][1]:
            winner = "draw"
        else:
            winner = most_common[0][0]

        self.voting_closed = True

        # ✅ DRAW CASE — refund stake!
        if winner == "draw":
            for p in self.players:
                pdata = await get_player(p)
                pdata["draws"] += 1
                pdata["games_played"] += 1
                pdata["current_streak"] = 0
                await save_player(p, pdata)

            for uid, uname, amount, choice in self.game_view.bets:
                # Refund the original stake
                await add_credits_internal(uid, amount)
                # Mark bet as neutral (no win/loss)
                await run_db(lambda: supabase
                    .table("bets")
                    .update({"won": None})
                    .eq("player_id", uid)
                    .eq("game_id", self.game_view.message.id)
                    .eq("choice", choice)
                    .execute()
                )
                print(f"↩️ Refunded {amount} to {uname} (DRAW)")

            embed = await self.game_view.build_embed(self.message.guild, winner=winner)
            embed.set_footer(text="🎮 Game has ended: 🤝 Draw")
            await self.message.edit(embed=embed, view=None)

            if self.lobby_message:
                lobby_embed = await self.game_view.build_embed(self.lobby_message.guild, winner=winner)
                lobby_embed.set_footer(text="🎮 Game has ended: 🤝 Draw")
                await self.lobby_message.edit(embed=lobby_embed, view=None)

            await self.message.channel.send("🤝 Voting ended in a **draw** — all bets refunded.")
            await asyncio.sleep(30)
            await self.message.channel.edit(archived=True)
            return

        # ✅ WINNER CASE — update stats
        for p in self.players:
            pdata = await get_player(p)
            pdata["games_played"] += 1

            is_winner = (
                winner == p
                or (winner == "Team A" and p in self.players[:2])
                or (winner == "Team B" and p in self.players[2:])
            )

            if is_winner:
                pdata["rank"] += 10
                pdata["trophies"] += 1
                pdata["wins"] += 1
                pdata["current_streak"] += 1
                pdata["best_streak"] = max(pdata.get("best_streak", 0), pdata["current_streak"])
            else:
                pdata["rank"] -= 10
                pdata["losses"] += 1
                pdata["current_streak"] = 0

            await save_player(p, pdata)

        # ✅ Resolve bets
        for uid, uname, amount, choice in self.game_view.bets:
            won = False

            if self.game_type == "singles":
                winner_id = winner if isinstance(winner, int) else None
                won = (choice == "1" and self.players[0] == winner_id) or (choice == "2" and self.players[1] == winner_id)
            elif self.game_type == "doubles":
                won = winner == choice.upper()
            elif self.game_type == "triples":
                try:
                    index = int(choice) - 1
                    won = self.players[index] == winner
                except:
                    won = False

            # Mark win/loss in bets table
            await run_db(lambda: supabase
                .table("bets")
                .update({"won": won})
                .eq("player_id", uid)
                .eq("game_id", self.game_view.message.id)
                .eq("choice", choice)
                .execute()
            )

            if won:
                odds = await self.game_view.get_odds(choice)
                profit = int(amount / odds) if odds > 0 else amount
                payout = profit + amount  # profit + stake back
                await add_credits_internal(uid, payout)
                print(f"💰 {uname} won! Payout: {payout} (bet {amount}, profit {profit})")
            else:
                # Lost — stake was already deducted at bet time
                print(f"❌ {uname} lost {amount} (stake was upfront)")

        # ✅ Announce winner
        if isinstance(winner, int):
            member = self.message.guild.get_member(winner)
            winner_name = member.display_name if member else f"User {winner}"
        else:
            winner_name = winner

        embed = await self.game_view.build_embed(self.message.guild, winner=winner)
        embed.set_footer(text=f"🎮 Game has ended. Winner: {winner_name}")
        await self.message.edit(embed=embed, view=None)

        if self.lobby_message:
            lobby_embed = await self.game_view.build_embed(self.lobby_message.guild, winner=winner)
            lobby_embed.set_footer(text=f"🎮 Game has ended. Winner: {winner_name}")
            await self.lobby_message.edit(embed=lobby_embed, view=None)

        await self.message.channel.send(f"🏁 Voting ended. Winner: **{winner_name}**")
        await asyncio.sleep(30)
        await self.message.channel.edit(archived=True)

        if self.game_view and self.game_view.on_tournament_complete:
            if self.game_type == "singles" and isinstance(winner, int):
                await self.game_view.on_tournament_complete(winner)


class GameEndedButton(discord.ui.Button):
    def __init__(self, view):
        super().__init__(label="Game Ended", style=discord.ButtonStyle.danger)
        self.view_obj = view  # this is your RoomView

    async def callback(self, interaction: discord.Interaction):
        self.view_obj.game_has_ended = True
        self.view_obj.betting_closed = True

        # ✅ Update the THREAD embed to mark ended
        thread_embed = self.view_obj.lobby_embed.copy()
        thread_embed.set_footer(text="🎮 Game has ended.")
        await self.view_obj.message.edit(embed=thread_embed, view=None)

        # ✅ Start the voting phase
        await self.view_obj.start_voting()
        await interaction.response.defer()

        # ✅ Update the MAIN LOBBY embed to mark ended
        if self.view_obj.lobby_message:
            game_view = self.view_obj.game_view

            # Rebuild from DB — note winner param is for display only
            updated_embed = await game_view.build_embed(self.view_obj.lobby_message.guild, winner="ended")
            updated_embed.set_footer(text="🎮 Game has ended.")

            # 🔧 Remove betting buttons only
            to_remove = [
                item for item in game_view.children
                if isinstance(item, BettingButton) or getattr(item, 'label', '') == 'Place Bet'
            ]
            for item in to_remove:
                game_view.remove_item(item)

            await self.view_obj.lobby_message.edit(embed=updated_embed, view=game_view)


class VoteButton(discord.ui.Button):
    def __init__(self, value, view, label):
        super().__init__(label=label, style=discord.ButtonStyle.primary)
        self.value = value               # the vote choice (player ID or Team A/B)
        self.view_obj = view             # the RoomView instance

    async def callback(self, interaction: discord.Interaction):
        if self.view_obj.voting_closed:
            await interaction.response.send_message("❌ Voting has ended.", ephemeral=True)
            return

        # ✅ Save the vote in the RoomView memory
        self.view_obj.votes[interaction.user.id] = self.value

        # ✅ Optional: You can store this vote in Supabase too if you want an audit log
        # Example: await run_db(lambda: supabase.table("votes").insert({...})

        # ✅ Prepare feedback text
        voter = interaction.guild.get_member(interaction.user.id)
        if isinstance(self.value, int):
            voted_for = interaction.guild.get_member(self.value)
            voted_name = voted_for.display_name if voted_for else f"User {self.value}"
        else:
            voted_name = self.value

        await interaction.response.send_message(
            f"✅ {voter.display_name} voted for **{voted_name}**.",
            ephemeral=False
        )

        # ✅ Mark this player as free to join other games again
        player_manager.deactivate(interaction.user.id)

        # ✅ Optionally update player data in Supabase (for advanced audit)
        # For example, you could store that this user has voted, or log timestamp.

        # ✅ If everyone voted, finalize immediately
        if len(self.view_obj.votes) == len(self.view_obj.players):
            await self.view_obj.finalize_game()

# Tournament View Class
class TournamentView(discord.ui.View):
    def __init__(self, creator, max_players=None):
        super().__init__(timeout=None)
        self.creator = creator
        self.players = [creator]
        self.max_players = max_players
        self.message = None
        self.abandon_task = asyncio.create_task(self.abandon_if_not_filled())
        self.bets = []  # Store bets for the tournament

    async def abandon_tournament(self, reason):
        """Handle abandonment of the tournament"""
        global pending_game
        pending_games["tournament"] = None

        for p in self.players:
            player_manager.deactivate(p)

        embed = discord.Embed(
            title="❌ Tournament Abandoned",
            description=reason,
            color=discord.Color.red()
        )
        if self.message:
            await self.message.edit(embed=embed, view=None)

        # Reset for new games
        await start_new_game_button(self.message.channel, "tournament")

    async def abandon_if_not_filled(self):
        """Automatically abandon the tournament if it's not filled within 5 minutes"""
        await asyncio.sleep(300)
        if len(self.players) < self.max_players:
            await self.abandon_tournament("⏰ Tournament timed out due to inactivity.")
            await clear_pending_game("tournament")

    async def build_embed(self, guild=None):
        """Build and return the embed for the tournament lobby."""
        embed = discord.Embed(
            title=f"🏆 Tournament Lobby",
            description="Players joining the tournament...",
            color=discord.Color.gold()
        )
        embed.set_author(
            name="LEAGUE OF EXTRAORDINARY MISFITS",
            icon_url="https://cdn.discordapp.com/attachments/1378860910310854666/1382601173932183695/LOGO_2.webp"
        )
        embed.timestamp = discord.utils.utcnow()

        player_lines = []
        for idx in range(self.max_players or 2):  # Default max players if not set
            if idx < len(self.players):
                user_id = self.players[idx]
                member = guild.get_member(user_id) if guild else None
                name = f"**{member.display_name}**" if member else f"**User {user_id}**"
                line = f"● Player {idx + 1}: {name}"
            else:
                line = f"○ Player {idx + 1}: [Waiting...]"
            player_lines.append(line)

        embed.add_field(name="👥 Players", value="\n".join(player_lines), inline=False)

        # Show the current bets placed
        if self.bets:
            bet_lines = [f"💰 **{uname}** bet **{amt}** on **{choice}**" for _, uname, amt, choice in self.bets]
            embed.add_field(name="📊 Current Bets", value="\n".join(bet_lines), inline=False)

        return embed

    async def update_message(self):
        """Update the tournament message."""
        if self.message:
            embed = await self.build_embed(self.message.guild)
            to_remove = [item for item in self.children if isinstance(item, discord.ui.Button)]
            for item in to_remove:
                self.remove_item(item)

            if len(self.players) < self.max_players:
                self.add_item(TournamentJoinButton(self))
            
            await self.message.edit(embed=embed, view=self)

    @discord.ui.button(label="Join Tournament", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handles player joining the tournament"""
        if interaction.user.id in self.players:
            await interaction.response.send_message("✅ You already joined.", ephemeral=True)
            return
        if len(self.players) >= self.max_players:
            await interaction.response.send_message("🚫 Tournament is full.", ephemeral=True)
            return
        if player_manager.is_active(interaction.user.id):
            await interaction.response.send_message("🚫 You’re already in another game.", ephemeral=True)
            return

        player_manager.activate(interaction.user.id)
        self.players.append(interaction.user.id)
        await self.update_message()
        await interaction.response.defer()

        if len(self.players) == self.max_players:
            await self.update_message()
            await self.tournament_full(interaction)

    async def tournament_full(self, interaction):
        """Trigger when the tournament is full and ready to start"""
        global pending_game
        self.clear_items()
        if self.abandon_task:
            self.abandon_task.cancel()

        await start_new_game_button(self.message.channel, "tournament")
        pending_games["tournament"] = None

        # Start tournament after betting phase
        await self.start_tournament(interaction)

    async def start_tournament(self, interaction):
        """Start the tournament logic"""
        embed = discord.Embed(
            title="🏁 Tournament Started!",
            description="Bracket generation and matches will begin shortly.",
            color=discord.Color.green()
        )
        await self.message.edit(embed=embed, view=None)

        # Initialize tournament
        tourney = Tournament(
            host_id=self.creator,
            players=self.players,
            channel=interaction.channel
        )
        await tourney.start()

    async def show_betting_phase(self):
        """Displays the betting phase after tournament starts"""
        self.clear_items()
        self.add_item(BettingButtonDropdown(self))  # Use your existing BettingButtonDropdown
        await self.update_message()
        await asyncio.sleep(120)  # Betting duration
        self.betting_closed = True
        self.clear_items()
        await self.update_message()


class TournamentStartButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Start Tournament", style=discord.ButtonStyle.primary)
    async def start_tournament(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle the start of tournament button click"""
        # Create a modal to select the number of players
        await interaction.response.send_modal(PlayerCountModal())
        
class PlayerCountModal(discord.ui.Modal, title="Select Number of Players"):
    def __init__(self):
        super().__init__()
        self.player_count = discord.ui.TextInput(
            label="Enter the number of players (4, 6 or 8.)",
            placeholder="4, 6, 8",
            max_length=4
        )
        self.add_item(self.player_count)

    async def on_submit(self, interaction: discord.Interaction):
        """Submit the number of players"""
        try:
            count = int(self.player_count.value.strip())
            if count < 2 or (count & (count - 1)) != 0:  # Must be a power of 2
                raise ValueError("Invalid player count. Must be a power of 2.")
        except ValueError:
            await interaction.response.send_message("❌ Invalid player count. Must be a power of 2.", ephemeral=True)
            return

        # Initialize Tournament View with max_players set dynamically
        game_view = GameView(game_type="tournament", creator=interaction.user.id, max_players=count)
        embed = await game_view.build_embed(interaction.guild)
        
        # Send the game message with the updated max_players
        game_view.message = await interaction.channel.send(embed=embed, view=game_view)

        await interaction.response.send_message(f"✅ Tournament will have **{count} players**! Players can now join.", ephemeral=True)


class Tournament:
    def __init__(self, host_id, players, channel, game_type="singles"):
        self.host_id = host_id
        self.players = players
        self.channel = channel
        self.game_type = game_type
        self.round = 1
        self.bracket = []
        self.current_matches = []
        self.thread = None

    async def start(self):
        # Create main tournament thread
        self.thread = await self.channel.create_thread(name=f"Tournament {random.randint(1000, 9999)}")

        await self.thread.send(f"🏆 **Tournament started!** Players: {', '.join(f'<@{p}>' for p in self.players)}")
        await self.next_round()

    async def next_round(self):
        if len(self.players) == 1:
            await self.thread.send(f"🎉 **Winner: <@{self.players[0]}>!**")
            return

        random.shuffle(self.players)
        self.current_matches = []

        for i in range(0, len(self.players), 2):
            if i+1 < len(self.players):
                match_players = [self.players[i], self.players[i+1]]
                view = GameView("singles", match_players[0])
                view.players = match_players
                view.max_players = 2
                embed = await view.build_embed(self.channel.guild)
                msg = await self.thread.send(embed=embed, view=view)
                view.message = msg
                view.on_tournament_complete = self.match_complete  # Hook!
                self.current_matches.append(view)
            else:
                # Odd player advances automatically
                await self.thread.send(f"✅ <@{self.players[i]}> advances (bye).")
                self.current_matches.append(self.players[i])

    async def match_complete(self, winner_id):
        self.players = [winner_id if isinstance(m, GameView) and m.winner == winner_id else p
                        for m, p in zip(self.current_matches, self.players)]
        await self.next_round()


class BetAmountModal(discord.ui.Modal, title="Enter Bet Amount"):
    def __init__(self, choice, game_view):
        super().__init__()
        self.choice = choice
        self.game_view = game_view

        self.bet_amount = discord.ui.TextInput(
            label="Bet Amount",
            placeholder="E.g. 100",
            required=True
        )
        self.add_item(self.bet_amount)

    async def on_submit(self, interaction: discord.Interaction):
        user_id = interaction.user.id

        try:
            amount = int(self.bet_amount.value.strip())
            if amount <= 0:
                raise ValueError()
        except ValueError:
            await interaction.response.send_message("❌ Invalid amount.", ephemeral=True)
            return

        # ✅ Compute odds & payout
        odds = await self.game_view.get_odds(self.choice)
        payout = int(amount / odds) if odds > 0 else amount

        # ✅ Atomic deduction
        success = await deduct_credits_atomic(user_id, amount)
        if not success:
            await interaction.response.send_message("❌ Not enough credits.", ephemeral=True)
            return

        # ✅ Log bet
        await run_db(lambda: supabase.table("bets").insert({
            "player_id": str(user_id),
            "game_id": self.game_view.message.id,
            "choice": self.choice,
            "amount": amount,
            "payout": payout,
            "won": None
        }).execute())

        # ✅ Add to UI
        await self.game_view.add_bet(user_id, interaction.user.display_name, amount, self.choice)

        await interaction.response.send_message(
            f"✅ Bet of **{amount}** on **{self.choice}** placed!\n📊 Odds: {odds * 100:.1f}% | 💰 Payout: **{payout}**",
            ephemeral=True
        )



class BettingDropdownView(discord.ui.View):
    def __init__(self, game_view):
        super().__init__(timeout=60)
        self.dropdown = BetDropdown(game_view)
        self.add_item(self.dropdown)

    async def prepare(self):
        await self.dropdown.build_options()


class LeaderboardView(discord.ui.View):
    def __init__(self, entries, page_size=10, sort_key="rank", title="🏆 Leaderboard"):
        super().__init__(timeout=120)
        self.entries = entries
        self.page_size = page_size
        self.sort_key = sort_key
        self.title = title
        self.page = 0
        self.message = None
        self.update_buttons()

    def update_buttons(self):
        self.clear_items()
        if self.page > 0:
            self.add_item(self.PreviousButton(self))
        if (self.page + 1) * self.page_size < len(self.entries):
            self.add_item(self.NextButton(self))

    def format_page(self):
        start = self.page * self.page_size
        end = start + self.page_size
        lines = []
        for i, (uid, stats) in enumerate(self.entries[start:end], start=start + 1):
            name = f"<@{uid}>"
            rank = stats.get("rank", 1000)
            trophies = stats.get("trophies", 0)
            credits = stats.get("credits", 1000)
            line = f"#{i:>2}  {name:<20} | 🏆 {trophies:<3} | 💰 {credits:<4} | 📈 {rank}"
            lines.append(line)
        return "\n".join(lines) if lines else "*No entries*"

    async def update(self):
        self.update_buttons()
        embed = discord.Embed(
            title=self.title,
            description=self.format_page(),
            color=discord.Color.gold()
        )
        await self.message.edit(embed=embed, view=self)

    class PreviousButton(discord.ui.Button):
        def __init__(self, view_obj):
            super().__init__(label="⬅ Previous", style=discord.ButtonStyle.secondary)
            self.view_obj = view_obj

        async def callback(self, interaction: discord.Interaction):
            self.view_obj.page = max(0, self.view_obj.page - 1)
            await self.view_obj.update()
            await interaction.response.defer()

    class NextButton(discord.ui.Button):
        def __init__(self, view_obj):
            super().__init__(label="Next ➡", style=discord.ButtonStyle.secondary)
            self.view_obj = view_obj

        async def callback(self, interaction: discord.Interaction):
            self.view_obj.page += 1
            await self.view_obj.update()
            await interaction.response.defer()

class PaginatedCourseSelect(discord.ui.Select):
    def __init__(self, options, parent_view):
        super().__init__(placeholder="Select a course", options=options)
        self.view_obj = parent_view

    async def callback(self, interaction: discord.Interaction):
        course_id = self.values[0]
        selected = next((c for c in self.view_obj.courses if str(c["id"]) == course_id), None)
        if not selected:
            await interaction.response.send_message("❌ Course not found.", ephemeral=True)
            return

        await interaction.response.send_modal(
            SubmitScoreModal(course_name=selected["name"], course_id=course_id)
        )


class PaginatedCourseView(discord.ui.View):
    def __init__(self, courses, per_page=25):
        super().__init__(timeout=120)
        self.courses = courses
        self.per_page = per_page
        self.page = 0
        self.message = None
        self.update_children()

    def update_children(self):
        self.clear_items()
        start = self.page * self.per_page
        end = start + self.per_page
        page_courses = self.courses[start:end]

        options = [
            discord.SelectOption(label=c["name"], value=str(c["id"]))
            for c in page_courses
        ]
        self.add_item(PaginatedCourseSelect(options, self))

        if self.page > 0:
            self.add_item(self.PrevButton(self))
        if end < len(self.courses):
            self.add_item(self.NextButton(self))

    async def update(self):
        self.update_children()
        await self.message.edit(view=self)

    class PrevButton(discord.ui.Button):
        def __init__(self, view):
            super().__init__(label="⬅ Previous", style=discord.ButtonStyle.secondary)
            self.view_obj = view

        async def callback(self, interaction: discord.Interaction):
            self.view_obj.page -= 1
            await self.view_obj.update()
            await interaction.response.defer()

    class NextButton(discord.ui.Button):
        def __init__(self, view):
            super().__init__(label="Next ➡", style=discord.ButtonStyle.secondary)
            self.view_obj = view

        async def callback(self, interaction: discord.Interaction):
            self.view_obj.page += 1
            await self.view_obj.update()
            await interaction.response.defer()


class SubmitScoreModal(discord.ui.Modal, title="Submit Score"):
    def __init__(self, course_name, course_id):
        super().__init__()
        self.course_name = course_name
        self.course_id = course_id

        self.add_item(discord.ui.TextInput(
            label=f"Best score for {course_name}",
            placeholder="Enter your best score (e.g. 72.5)",
            style=discord.TextStyle.short
        ))

    async def on_submit(self, interaction: Interaction):
        try:
            score = float(self.children[0].value)
        except ValueError:
            await interaction.response.send_message("❌ Invalid number.", ephemeral=True)
            return

        # ✅ Fetch course info from DB
        course = await run_db(lambda: supabase
            .table("courses")
            .select("*")
            .eq("id", self.course_id)
            .single()
            .execute()
        )

        # ✅ Get course_rating & slope_rating with safe defaults
        course_rating = float(course.data.get("course_rating", 72.0))
        slope_rating = float(course.data.get("slope_rating", 113.0))

        # ✅ Calculate differential using official formula:
        # (Score - Course Rating) * 113 / Slope Rating
        differential = round((score - course_rating) * 113 / slope_rating, 1)

        # ✅ Upsert full data to `handicaps` table
        await run_db(lambda: supabase
            .table("handicaps")
            .upsert({
                "player_id": str(interaction.user.id),
                "course_id": self.course_id,
                "course_name": self.course_name,
                "score": score,
                "course_rating": course_rating,
                "slope_rating": slope_rating,
                "handicap_differential": differential
            })
            .execute()
        )

        await interaction.response.send_message(
            f"✅ Submitted **{score}** for **{self.course_name}**\n"
            f"📏 Course Rating: `{course_rating}` | Slope: `{slope_rating}`\n"
            f"📊 Differential: `{differential}`",
            ephemeral=True
        )



@tree.command(name="submit_score", description="Submit your best score for a course")
async def submit_score(interaction: discord.Interaction):
    res = await run_db(lambda: supabase.table("courses").select("id, name").execute())
    if not res.data:
        await interaction.response.send_message("⚠️ No courses found.", ephemeral=True)
        return

    view = PaginatedCourseView(res.data)
    await interaction.response.send_message(
        "🏌️‍♂️ Select a course to submit your score (use Next/Prev if needed):",
        view=view,
        ephemeral=True
    )
    view.message = await interaction.original_response()



@tree.command(name="init_singles")
async def init_singles(interaction: discord.Interaction):
    """Creates a singles game lobby with the start button"""
    
    # 1️⃣ Fast RAM check FIRST:
    if pending_games["singles"] or any(k[0] == interaction.channel.id for k in start_buttons):
        await interaction.response.send_message(
            "⚠️ A singles game is already pending or a button is active here.",
            ephemeral=True
        )
        return

    # 2️⃣ Safe: defer because View is coming
    await interaction.response.defer(ephemeral=True)

    # 3️⃣ Create the button
    await start_new_game_button(interaction.channel, "singles")

    max_players = 2

    # 4️⃣ Create the GameView with max_players set to 2 for singles
    game_view = GameView(game_type="singles", creator=interaction.user.id, max_players=max_players)

    # 5️⃣ Send confirmation
    await interaction.followup.send(
        "✅ Singles game button posted and ready for players to join!",
        ephemeral=True
    )


async def init_doubles(interaction: discord.Interaction):
    """Creates a doubles game lobby with the start button"""
    
    # 1️⃣ Fast RAM check FIRST:
    if pending_games["doubles"] or any(k[0] == interaction.channel.id for k in start_buttons):
        await interaction.response.send_message(
            "⚠️ A doubles game is already pending or a button is active here.",
            ephemeral=True
        )
        return

    # 2️⃣ Safe: defer because View is coming
    await interaction.response.defer(ephemeral=True)

    # 3️⃣ Create the button
    await start_new_game_button(interaction.channel, "doubles")

    max_players = 4
    # 4️⃣ Create the GameView with max_players set to 2 for doubles
    game_view = GameView(game_type="doubles", creator=interaction.user.id, max_players=max_players)

    # 5️⃣ Send confirmation
    await interaction.followup.send(
        "✅ Doubles game button posted and ready for players to join!",
        ephemeral=True
    )



async def init_triples(interaction: discord.Interaction):
    """Creates a doubles game lobby with the start button"""
    
    # 1️⃣ Fast RAM check FIRST:
    if pending_games["triples"] or any(k[0] == interaction.channel.id for k in start_buttons):
        await interaction.response.send_message(
            "⚠️ A triples game is already pending or a button is active here.",
            ephemeral=True
        )
        return

    # 2️⃣ Safe: defer because View is coming
    await interaction.response.defer(ephemeral=True)

    # 3️⃣ Create the button
    await start_new_game_button(interaction.channel, "triples")

    max_players = 3

    # 4️⃣ Create the GameView with max_players set to 2 for triples
    game_view = GameView(game_type="triples", creator=interaction.user.id, max_players=max_players)

    # 5️⃣ Send confirmation
    await interaction.followup.send(
        "✅ Triples game button posted and ready for players to join!",
        ephemeral=True
    )



@tree.command(
    name="leaderboard",
    description="Show the ELO leaderboard or stats for a specific user"
)
@discord.app_commands.describe(user="User to check in the leaderboard")
async def leaderboard_local(interaction: discord.Interaction, user: discord.User = None):
    # 1️⃣ Fetch all players ordered by rank descending
    res = await run_db(lambda: supabase.table("players").select("*").order("rank", desc=True).execute())
    if res.data is None:
        await interaction.response.send_message("📭 No players have stats yet.", ephemeral=True)
        return

    sorted_stats = res.data

    # 2️⃣ If specific user, show their rank entry
    if user:
        user_id = user.id
        rank = next((i + 1 for i, row in enumerate(sorted_stats) if row["id"] == user_id), None)
        if rank is None:
            await interaction.response.send_message(f"⚠️ {user.display_name} is not on the leaderboard.", ephemeral=True)
            return

        stats = next(row for row in sorted_stats if row["id"] == user_id)
        elo = stats.get("rank", 1000)
        trophies = stats.get("trophies", 0)
        badge = "🥇" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else ""

        line = f"#{rank:>2}  {user.display_name[:20]:<20} | {elo:<4} | 🏆 {trophies} {badge}"
        embed = discord.Embed(
            title=f"📊 Leaderboard Entry for {user.display_name}",
            description=line,
            color=discord.Color.blue()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # 3️⃣ Otherwise, show paginated leaderboard

    class LeaderboardView(discord.ui.View):
        def __init__(self, entries, per_page=10):
            super().__init__(timeout=120)
            self.entries = entries
            self.per_page = per_page
            self.page = 0
            self.message = None
            self.update_buttons()

        def update_buttons(self):
            self.clear_items()
            if self.page > 0:
                self.add_item(self.Prev(self))
            if (self.page + 1) * self.per_page < len(self.entries):
                self.add_item(self.Next(self))

        def format_embed(self, guild):
            start = self.page * self.per_page
            end = start + self.per_page
            embed = discord.Embed(
                title="🏆 Leaderboard",
                color=discord.Color.gold()
            )
            for i, row in enumerate(self.entries[start:end], start=start + 1):
                member = guild.get_member(int(row["id"]))
                name = member.display_name if member else f"User {row['id']}"
                elo = row.get("rank", 1000)
                trophies = row.get("trophies", 0)
                badge = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else ""
                embed.add_field(
                    name=f"#{i} {name}",
                    value=f"ELO: {elo} | 🏆 {trophies} {badge}",
                    inline=False
                )
            return embed

        async def update(self):
            self.update_buttons()
            embed = self.format_embed(self.message.guild)
            await self.message.edit(embed=embed, view=self)

        class Prev(discord.ui.Button):
            def __init__(self, view):
                super().__init__(label="⬅ Prev", style=discord.ButtonStyle.secondary)
                self.v = view

            async def callback(self, i):
                self.v.page -= 1
                await self.v.update()
                await i.response.defer()

        class Next(discord.ui.Button):
            def __init__(self, view):
                super().__init__(label="Next ➡", style=discord.ButtonStyle.secondary)
                self.v = view

            async def callback(self, i):
                self.v.page += 1
                await self.v.update()
                await i.response.defer()

    # Create view and send
    view = LeaderboardView(sorted_stats)
    first_embed = view.format_embed(interaction.guild)
    msg = await interaction.response.send_message(embed=first_embed, view=view)
    view.message = await msg.original_response()

@tree.command(
    name="stats_reset",
    description="Admin: Reset a user's stats"
)
@app_commands.describe(user="The user to reset")
@discord.app_commands.checks.has_permissions(administrator=True)
async def stats_reset(interaction: discord.Interaction, user: discord.User):
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)

    try:
        # ✅ Create fresh default stats
        new_stats = default_template.copy()
        new_stats["id"] = str(user.id)  # Make sure ID type matches your table

        # ✅ Upsert: insert or overwrite in `players` table
        res = await run_db(lambda: supabase
            .table("players")
            .upsert(new_stats)
            .execute()
        )

        if getattr(res, "status_code", 200) != 200:
            await interaction.followup.send(
                f"❌ Failed to reset stats: {getattr(res, 'data', res)}",
                ephemeral=True
            )
            return

        await interaction.followup.send(
            f"✅ Stats for {user.display_name} have been reset (bet history untouched).",
            ephemeral=True
        )

    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)



@tree.command(
    name="stats",
    description="Show player stats"
)
@app_commands.describe(
    user="User to show stats for (leave blank for yourself)",
    dm="Send results as DM"
)
async def stats(interaction: discord.Interaction, user: discord.User = None, dm: bool = False):
    await interaction.response.defer(ephemeral=True)

    target_user = user or interaction.user

    res = await run_db(lambda: supabase.table("players").select("*").eq("id", str(target_user.id)).single().execute())

    if res.data is None:
        player = default_template.copy()
    else:
        player = res.data

    wins = player.get("wins", 0)
    losses = player.get("losses", 0)
    draws = player.get("draws", 0)
    games = player.get("games_played", 0)
    trophies = player.get("trophies", 0)
    streak = player.get("current_streak", 0)
    best_streak = player.get("best_streak", 0)
    rank = player.get("rank", 1000)
    credits = player.get("credits", 1000)

    bets_res = await run_db(lambda: supabase.table("bets").select("*").eq("player_id", str(target_user.id)).order("id", desc=True).limit(5).execute())
    all_bets_res = await run_db(lambda: supabase.table("bets").select("id,won,payout,amount").eq("player_id", str(target_user.id)).execute())

    total_bets = len(all_bets_res.data or [])
    bets_won = sum(1 for b in all_bets_res.data if b.get("won") is True)
    bets_lost = sum(1 for b in all_bets_res.data if b.get("won") is False)
    net_gain = sum(b.get("payout", 0) - b.get("amount", 0) for b in all_bets_res.data if b.get("won") is not None)

    embed = discord.Embed(title=f"📊 Stats for {target_user.display_name}", color=discord.Color.blue())
    embed.add_field(name="🏆 Trophies", value=trophies)
    embed.add_field(name="📈 Rank", value=rank)
    embed.add_field(name="💰 Credits", value=credits)
    embed.add_field(name="\u200b", value="\u200b", inline=False)
    embed.add_field(name="🎮 Games Played", value=games)
    embed.add_field(name="✅ Wins", value=wins)
    embed.add_field(name="❌ Losses", value=losses)
    embed.add_field(name="➖ Draws", value=draws)
    embed.add_field(name="🔥 Current Streak", value=streak)
    embed.add_field(name="🏅 Best Streak", value=best_streak)
    embed.add_field(name="\u200b", value="\u200b", inline=False)
    embed.add_field(name="🪙 Total Bets", value=total_bets)
    embed.add_field(name="✅ Bets Won", value=bets_won)
    embed.add_field(name="❌ Bets Lost", value=bets_lost)
    embed.add_field(name="💸 Net Gain/Loss", value=f"{net_gain:+}")

    # ✅ Safe bet history with clear draw logic:
    if bets_res.data:
        lines = []
        for b in bets_res.data:
            won = b.get("won")
            if won is True:
                result = f"Won ✅ {b.get('amount')} on {b.get('choice')} (Payout: {b.get('payout')})"
            elif won is False:
                result = f"Lost ❌ {b.get('amount')} on {b.get('choice')} (Payout: 0)"
            else:
                result = f"Draw ⚪️ {b.get('amount')} on {b.get('choice')} (No payout)"
            lines.append(result)

        embed.add_field(name="🗓️ Recent Bets", value="\n".join(lines), inline=False)

    if dm:
        try:
            await target_user.send(embed=embed)
            await interaction.followup.send("✅ Stats sent via DM!", ephemeral=True)
        except Exception:
            await interaction.followup.send("⚠️ Could not send DM.", ephemeral=True)
    else:
        await interaction.followup.send(embed=embed, ephemeral=True)



@tree.command(
    name="clear_active",
    description="Admin: Clear all pending games, start buttons, or only a specific user's active state."
)
@app_commands.describe(
    user="User to clear from active players (optional)"
)
@discord.app_commands.checks.has_permissions(administrator=True)
async def clear_active(interaction: discord.Interaction, user: discord.User = None):
    try:
        # ✅ Always defer immediately, no condition check needed
        await interaction.response.defer(ephemeral=True)

        if user:
            # ✅ Deactivate only this user
            player_manager.deactivate(user.id)
            await interaction.followup.send(
                f"✅ Cleared active status for {user.display_name}.",
                ephemeral=True
            )
            return

        # ✅ Clear all pending games
        for key in pending_games:
            pending_games[key] = None

        # ✅ Clear all active players
        player_manager.clear()

        # ✅ Delete all start buttons safely
        for msg in list(start_buttons.values()):
            try:
                await msg.delete()
            except Exception:
                pass
        start_buttons.clear()

        await interaction.followup.send(
            "✅ Cleared ALL pending games, active players, and start buttons.",
            ephemeral=True
        )

    except Exception as e:
        # If something fails AFTER deferring, fallback to followup
        await interaction.followup.send(f"⚠️ Failed: {e}", ephemeral=True)


@tree.command(
    name="stats_edit",
    description="Admin command to edit a user's stats"
)
@app_commands.describe(
    user="User to edit",
    field="Field to change (rank, trophies, credits)",
    value="New value"
)
async def stats_edit(interaction: discord.Interaction, user: discord.User, field: str, value: int):
    # ✅ Check admin permissions
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "⛔ You don't have permission to use this command.",
            ephemeral=True
        )
        return

    # ✅ Only allow editing whitelisted fields
    valid_fields = {"rank", "trophies", "credits"}
    if field not in valid_fields:
        await interaction.response.send_message(
            f"⚠️ Invalid field. Choose from: {', '.join(valid_fields)}",
            ephemeral=True
        )
        return

    # ✅ Upsert in Supabase
    update = {"id": str(user.id), field: value}
    res = await run_db(lambda: supabase.table("players").upsert(update).execute())

    if res.status_code != 201 and res.status_code != 200:
        await interaction.response.send_message(
            f"❌ Error updating stats. Status code: {res.status_code}",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"✅ Updated **{field}** for {user.display_name} to **{value}**.",
        ephemeral=True
    )


@tree.command(
    name="clear_chat",
    description="Admin: Delete all messages in this channel (last 14 days only)"
)
@discord.app_commands.checks.has_permissions(administrator=True)
async def clear_chat(interaction: discord.Interaction):
    try:
        # ✅ Check if the interaction is still valid
        if interaction.response.is_done():
            return

        await interaction.response.defer(ephemeral=True)

        channel = interaction.channel

        # ✅ Only text channels & threads that allow bulk delete
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.followup.send("❌ This command can only be used in text channels or threads.", ephemeral=True)
            return

        def not_pinned(msg):
            return not msg.pinned

        deleted = await channel.purge(limit=1000, check=not_pinned, bulk=True)

        # ✅ Remove stale start buttons in this channel
        for key in list(start_buttons.keys()):
            if key[0] == channel.id:
                del start_buttons[key]

        await interaction.followup.send(f"🧹 Cleared {len(deleted)} messages.", ephemeral=True)

    except Exception as e:
        # Fallback: interaction might be expired — so fallback to plain send
        try:
            if interaction.followup:
                await interaction.followup.send(f"⚠️ Error: {e}", ephemeral=True)
            else:
                await interaction.channel.send(f"⚠️ Error: {e}")
        except:
            pass



@tree.command(
    name="clear_pending",
    description="Admin: Clear all pending games and remove start buttons."
)
async def clear_pending(interaction: discord.Interaction):
    # ✅ Check admin permissions
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "⛔ You must be an admin to use this.",
            ephemeral=True
        )
        return

    # 1️⃣ Clear local `pending_games` state
    for key in pending_games:
        pending_games[key] = None

    # 2️⃣ Clear Supabase `pending_games` table
    await run_db(lambda: supabase.table("pending_games").delete().neq("game_type", "").execute())

    # 3️⃣ Delete any start buttons messages
    for msg in list(start_buttons.values()):
        try:
            await msg.delete()
        except Exception:
            pass

    # 4️⃣ Clear local `start_buttons` dict
    start_buttons.clear()

    await interaction.response.send_message(
        "✅ All pending games and start buttons have been cleared.",
        ephemeral=True
    )


@tree.command(
    name="add_credits",
    description="Admin command to add credits to a user"
)
@app_commands.describe(
    user="User to add credits to",
    amount="Amount of credits to add"
)
async def add_credits(interaction: discord.Interaction, user: discord.User, amount: int):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "⛔ You don't have permission to use this command.",
            ephemeral=True
        )
        return

    player = await get_player(user.id)
    new_credits = player.get("credits", 0) + amount

    await run_db(lambda: supabase.table("players").update({"credits": new_credits}).eq("id", str(user.id)).execute())

    await interaction.response.send_message(
        f"✅ Added {amount} credits to {user.display_name}. New total: {new_credits}.",
        ephemeral=True
    )


@tree.command(name="init_tournament")
async def init_tournament(interaction: discord.Interaction):
    # 1️⃣ Fast RAM check FIRST:
    if pending_games["tournament"] or any(k[0] == interaction.channel.id for k in start_buttons):
        await interaction.response.send_message(
            "⚠️ A tournament game is already pending or a button is active here.",
            ephemeral=True
        )
        return

    # 2️⃣ Safe: defer because View is coming
    await interaction.response.defer(ephemeral=True)

    # 3️⃣ Create the button
    await start_new_game_button(interaction.channel, "tournament")

    # 4️⃣ Confirm
    await interaction.followup.send(
        "✅ Tournament game button posted!",
        ephemeral=True
    )


@tree.command(
    name="clear_bet_history",
    description="Admin: Clear a user's entire betting history without changing other stats"
)
@app_commands.describe(user="The user whose bets you want to clear")
@discord.app_commands.checks.has_permissions(administrator=True)
async def clear_bet_history(interaction: discord.Interaction, user: discord.User):
    # ✅ Always check .is_done()
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)

    try:
        # ✅ Delete all bets for this user
        res = await run_db(lambda: supabase
            .table("bets")
            .delete()
            .eq("player_id", str(user.id))
            .execute()
        )

        # ✅ Robust error check
        if hasattr(res, "status_code") and res.status_code != 200:
            msg = getattr(res, "data", str(res))
            await interaction.followup.send(
                f"❌ Failed to clear bet history: {msg}",
                ephemeral=True
            )
            return

        await interaction.followup.send(
            f"✅ Cleared **all betting history** for {user.display_name}.",
            ephemeral=True
        )

    except Exception as e:
        await interaction.followup.send(
            f"❌ Error while clearing bet history: `{e}`",
            ephemeral=True
        )

from discord import app_commands, Interaction, SelectOption, ui, Embed


@tree.command(
    name="handicap_index",
    description="Calculate your current handicap index (average of your best scores)"
)
async def handicap_index(interaction: discord.Interaction, user: discord.User = None):
    await interaction.response.defer(ephemeral=True)

    target = user or interaction.user

    res = await run_db(lambda: supabase
        .table("handicaps")
        .select("handicap_differential")
        .eq("player_id", str(target.id))
        .execute()
    )

    differentials = sorted([row["handicap_differential"] for row in res.data or []])
    count = min(len(differentials), 8)

    if count == 0:
        await interaction.followup.send(f"❌ No scores found for {target.display_name}.", ephemeral=True)
        return

    index = round(sum(differentials[:count]) / count, 1)

    await interaction.followup.send(
        f"🏌️ **{target.display_name}'s Handicap Index:** `{index}` "
        f"(average of best {count} differentials)",
        ephemeral=True
    )


@tree.command(
    name="my_handicaps",
    description="See all your submitted scores and handicap differentials"
)
async def my_handicaps(interaction: discord.Interaction, user: discord.User = None):
    # ⏱️ Defer immediately, before anything slow
    await interaction.response.defer(ephemeral=True)

    # Now safe to do slow things
    target = user or interaction.user

    try:
        res = await run_db(lambda: supabase
            .table("handicaps")
            .select("*")
            .eq("player_id", str(target.id))
            .order("score")
            .execute()
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Database error: {e}", ephemeral=True)
        return

    if not res.data:
        await interaction.followup.send(f"❌ No scores found for {target.display_name}.", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"🏌️ {target.display_name}'s Handicap Records",
        color=discord.Color.green()
    )

    for h in res.data:
        embed.add_field(
            name=f"{h['course_name']}",
            value=(
                f"Score: **{h['score']}**\n"
                f"Course Rating: **{h['course_rating']}**\n"
                f"Slope: **{h['slope_rating']}**\n"
                f"Differential: **{h['handicap_differential']}**"
            ),
            inline=False
        )

    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(
    name="handicap_leaderboard",
    description="Show the leaderboard of players ranked by handicap index"
)
async def handicap_leaderboard(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    # 1️⃣ Fetch ALL differentials for ALL players
    res = await run_db(lambda: supabase
        .table("handicaps")
        .select("player_id, handicap_differential")
        .execute()
    )

    if not res.data:
        await interaction.followup.send("❌ No handicap data found.", ephemeral=True)
        return

    # 2️⃣ Group by player and calculate their index
    from collections import defaultdict

    grouped = defaultdict(list)
    for row in res.data:
        grouped[row["player_id"]].append(row["handicap_differential"])

    leaderboard = []
    for pid, diffs in grouped.items():
        diffs.sort()
        count = min(len(diffs), 8)
        index = round(sum(diffs[:count]) / count, 1)
        leaderboard.append((pid, index))

    # 3️⃣ Sort by index ascending
    leaderboard.sort(key=lambda x: x[1])

    # 4️⃣ Build embed
    embed = discord.Embed(
        title="🏌️ Handicap Leaderboard",
        description="Players ranked by handicap index (lower is better!)",
        color=discord.Color.gold()
    )

    lines = []
    for rank, (pid, index) in enumerate(leaderboard, start=1):
        member = interaction.guild.get_member(int(pid))
        name = member.display_name if member else f"User {pid}"
        lines.append(f"**#{rank}** — {name} | Index: `{index}`")

    embed.description = "\n".join(lines)

    await interaction.followup.send(embed=embed, ephemeral=True)



@tree.command(name="dm_online")
@app_commands.describe(msg="Message to send")
@discord.app_commands.checks.has_permissions(administrator=True)
async def dm_online(interaction: discord.Interaction, msg: str):
    await interaction.response.send_message(
        f"📨 Sending message to online members...",
        ephemeral=True
    )
    await dm_all_online(interaction.guild, msg)
    await interaction.followup.send("✅ All online members have been messaged.", ephemeral=True)

@bot.event
async def on_ready():
    await tree.sync()
    print(f"✅ Logged in as {bot.user}")

    pending = await load_pending_games()
    for pg in pending:
        channel = bot.get_channel(pg["channel_id"])
        if channel:
            await start_new_game_button(channel, pg["game_type"])

bot.run(os.getenv("DISCORD_BOT_TOKEN"))
