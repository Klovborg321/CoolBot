import requests
import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import random
import json
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv
from supabase import create_client, Client
import os

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree

IS_TEST_MODE = os.getenv("TEST_MODE", "1") == "1"
start_buttons = {}  # (channel_id, game_type) => Message

# Globals
games = {}

pending_games = {
    "singles": None,
    "doubles": None,
    "triples": None
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
async def save_pending_game(game_type, players, channel_id):
    await supabase.table("pending_games").upsert({
        "game_type": game_type,
        "players": players,
        "channel_id": channel_id
    }).execute()

# ✅ Atomic: Deduct credits if enough
async def deduct_credits_atomic(user_id: int, amount: int) -> bool:
    # Use 'gte' filter for race-safe check
    update = await supabase.table("players") \
        .update({"credits": supabase.py_.sql(f"credits - {amount}")}) \
        .eq("id", str(user_id)) \
        .gte("credits", amount) \
        .execute()
    return update.count > 0

# ✅ Atomic: Add credits
async def add_credits_atomic(user_id: int, amount: int):
    await supabase.table("players").update({
        "credits": supabase.py_.sql(f"credits + {amount}")
    }).eq("id", str(user_id)).execute()

async def clear_pending_game(game_type):
    supabase.table("pending_games").delete().eq("game_type", game_type).execute()

async def load_pending_games():
    response = supabase.table("pending_games").select("*").execute()
    return response.data

async def handle_bet(interaction, user_id, choice, amount, odds, game_id):
    # ✅ Try atomic deduction
    success = await deduct_credits_atomic(user_id, amount)
    if not success:
        await interaction.response.send_message("❌ Not enough credits.", ephemeral=True)
        return

    # ✅ Log the bet
    payout = int(amount / odds) if odds > 0 else amount
    await supabase.table("bets").insert({
        "player_id": str(user_id),
        "game_id": game_id,
        "choice": choice,
        "amount": amount,
        "payout": payout,
        "won": None
    }).execute()

    await interaction.response.send_message(
        f"✅ Bet of {amount} placed on {choice}. Potential payout: {payout}",
        ephemeral=True
    )



async def get_complete_user_data(user_id):
    res = await supabase.table("players").select("*").eq("id", str(user_id)).single().execute()
    if res.error:
        # If not found, insert defaults!
        defaults = default_template.copy()
        defaults["id"] = user_id
        await supabase.table("players").insert(defaults).execute()
        return defaults
    return res.data


async def update_user_stat(user_id, key, value, mode="set"):
    res = await supabase.table("players").select("*").eq("id", str(user_id)).single().execute()
    if res.error:
        # Player missing, create fresh
        data = default_template.copy()
        data["id"] = user_id
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
    res = supabase.table("players").select("*").eq("id", str(user_id)).execute()  # ❌ no await here!

    if not res.data:
        # no row found → create one
        new_data = default_template.copy()
        new_data["id"] = str(user_id)
        supabase.table("players").insert(new_data).execute()  # ❌ no await here either!
        return new_data

    return res.data[0]



# ✅ Fully async: Save (upsert)
async def save_player(user_id: int, player_data: dict):
    player_data["id"] = str(user_id)
    await supabase.table("players").upsert(player_data).execute()



def calculate_elo(elo1, elo2, result):
    expected = 1 / (1 + 10 ** ((elo2 - elo1) / 400))
    return elo1 + 32 * (result - expected)

def player_display(user_id, data):
    player = data.get(str(user_id), {"rank": 1000, "trophies": 0})
    return f"<@{user_id}> | Rank: {player['rank']} | Trophies: {player['trophies']}"

async def start_new_game_button(channel, game_type):
    key = (channel.id, game_type)
    old = start_buttons.get(key)
    if old:
        try:
            await old.delete()
        except discord.NotFound:
            pass
    view = GameJoinView(game_type)
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
    def __init__(self, game_type):
        super().__init__(timeout=None)
        self.game_type = game_type

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

        view = GameView(self.game_type, interaction.user.id)
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
        await interaction.response.send_message(
            "Select who you want to bet on:",
            view=BettingDropdownView(self.game_view),
            ephemeral=True
        )

class GameView(discord.ui.View):
    def __init__(self, game_type, creator):
        super().__init__(timeout=None)
        self.game_type = game_type
        self.creator = creator
        self.players = [creator]
        self.max_players = 2 if game_type == "singles" else 4 if game_type == "doubles" else 3
        self.message = None
        self.betting_closed = False
        self.bets = []
        self.abandon_task = asyncio.create_task(self.abandon_if_not_filled())
        self.add_item(LeaveGameButton(self))

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
        await asyncio.sleep(300)
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
        await start_new_game_button(self.message.channel, self.game_type)
        pending_games[self.game_type] = None
        await save_pending_game(self.game_type, self.players, self.message.channel.id)

        res = supabase.table("courses").select("name", "image_url").execute()
        if res.error:
            course_name = "Unknown"
            course_image = ""
        else:
            chosen = random.choice(res.data)
            course_name = chosen["name"]
            course_image = chosen.get("image_url", "")
            await room_name_generator.get_unique_word()

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
        await supabase.table("bets").insert({
            "player_id": str(user_id),
            "game_id": self.game_view.message.id,
            "choice": choice,
            "amount": amount,
            "payout": payout,
            "won": None
        }).execute()

        # ✅ Update live bets in GameView
        await self.game_view.add_bet(user_id, interaction.user.display_name, amount, choice)

        await interaction.response.send_message(
            f"✅ Bet of **{amount}** on **{choice}** placed!\n📊 Odds: {odds * 100:.1f}% | 💰 Payout: **{payout}**",
            ephemeral=True
        )



class BetDropdown(discord.ui.Select):
    def __init__(self, game_view):
        self.game_view = game_view
        self.options_built = False  # Will build options async
        super().__init__(
            placeholder="Select who to bet on...",
            min_values=1,
            max_values=1,
            options=[]  # Will fill later
        )

    async def build_options(self):
        options = []
        players = self.game_view.players
        game_type = self.game_view.game_type
        guild = self.game_view.message.guild if self.game_view.message else None

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

        self.options = options
        self.options_built = True

    async def callback(self, interaction: discord.Interaction):
        if not self.options_built:
            await self.build_options()  # Shouldn't normally be needed; pre-built by BettingDropdownView

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
        await asyncio.sleep(300)
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

        # ✅ Update stats in DB
        if winner == "draw":
            for p in self.players:
                pdata = await get_player(p)
                pdata["draws"] = pdata.get("draws", 0) + 1
                pdata["games_played"] = pdata.get("games_played", 0) + 1
                pdata["current_streak"] = 0
                await save_player(p, pdata)

            if self.lobby_message:
                embed = await self.game_view.build_embed(self.lobby_message.guild, winner=winner)
                embed.set_footer(text="🎮 Game has ended. Result: 🤝 Draw")
                await self.lobby_message.edit(embed=embed, view=self.game_view)

            await self.message.channel.send("🤝 Voting ended in a **draw**!")
            await asyncio.sleep(30)
            await self.message.channel.edit(archived=True)
            return

        # ✅ If there is a winner:
        for p in self.players:
            pdata = await get_player(p)
            pdata["games_played"] = pdata.get("games_played", 0) + 1

            is_winner = (
                winner == p
                or (winner == "Team A" and p in self.players[:2])
                or (winner == "Team B" and p in self.players[2:])
            )

            if is_winner:
                pdata["rank"] = pdata.get("rank", 1000) + 10
                pdata["trophies"] = pdata.get("trophies", 0) + 1
                pdata["wins"] = pdata.get("wins", 0) + 1
                pdata["current_streak"] = pdata.get("current_streak", 0) + 1
                pdata["best_streak"] = max(pdata.get("best_streak", 0), pdata["current_streak"])
            else:
                pdata["rank"] = pdata.get("rank", 1000) - 10
                pdata["losses"] = pdata.get("losses", 0) + 1
                pdata["current_streak"] = 0

            await save_player(p, pdata)

        # ✅ Resolve bets
        for uid, uname, amount, choice in self.game_view.bets:
            user_id = str(uid)
            user_data = await get_player(user_id)
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

            for bet in user_data.get("bet_history", []):
                if bet.get("game") == self.game_view.message.id and bet.get("won") is None:
                    bet["won"] = won
                    if won:
                        payout = bet.get("payout", int(amount / self.game_view.get_odds(choice)))
                        user_data["credits"] = user_data.get("credits", 0) + payout
                        print(f"💰 {uname} won {payout} credits (bet {amount} on {choice})")

            await save_player(user_id, user_data)

        # ✅ Show winner
        if isinstance(winner, int):
            member = self.message.guild.get_member(winner)
            winner_name = member.display_name if member else f"User {winner}"
        else:
            winner_name = winner

        embed = await self.game_view.build_embed(self.message.guild, winner=winner)
        await self.message.edit(embed=embed, view=self)

        if self.game_view.message:
            lobby_embed = await self.game_view.build_embed(self.game_view.message.guild, winner=winner)
            lobby_embed.set_footer(text=f"🎮 Game has ended. Winner: {winner_name}")
            await self.game_view.message.edit(embed=lobby_embed, view=None)

        await self.message.channel.send(f"🏁 Voting ended. Winner: **{winner_name}**")
        await asyncio.sleep(30)
        await self.message.channel.edit(archived=True)


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
        # Example: await supabase.table("votes").insert({...})

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
        await supabase.table("bets").insert({
            "player_id": str(user_id),
            "game_id": self.game_view.message.id,
            "choice": self.choice,
            "amount": amount,
            "payout": payout,
            "won": None
        }).execute()

        # ✅ Add to UI
        await self.game_view.add_bet(user_id, interaction.user.display_name, amount, self.choice)

        await interaction.response.send_message(
            f"✅ Bet of **{amount}** on **{self.choice}** placed!\n📊 Odds: {odds * 100:.1f}% | 💰 Payout: **{payout}**",
            ephemeral=True
        )



class BettingDropdownView(discord.ui.View):
    def __init__(self, game_view):
        super().__init__(timeout=60)
        self.add_item(BetDropdown(game_view))

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
        return "\n".join(lines)

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
            self.view_obj.page -= 1
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


@tree.command(name="init_singles")
async def init_singles(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    # ✅ Block if:
    # 1) There is any pending game of this type
    # 2) There is ANY start button in this channel (any game type)
    if pending_games["singles"] or any(k[0] == interaction.channel.id for k in start_buttons):
        await interaction.followup.send(
            "⚠️ A game is already pending or a button is active here.",
            ephemeral=True
        )
        return

    await start_new_game_button(interaction.channel, "singles")
    await interaction.followup.send(
        "✅ Singles game button posted!",
        ephemeral=True
    )


@tree.command(name="init_doubles")
async def init_doubles(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if pending_games["doubles"] or any(k[0] == interaction.channel.id for k in start_buttons):
        await interaction.followup.send(
            "⚠️ A game is already pending or a button is active here.",
            ephemeral=True
        )
        return

    await start_new_game_button(interaction.channel, "doubles")
    await interaction.followup.send(
        "✅ Doubles game button posted!",
        ephemeral=True
    )


@tree.command(name="init_triples")
async def init_triples(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if pending_games["triples"] or any(k[0] == interaction.channel.id for k in start_buttons):
        await interaction.followup.send(
            "⚠️ A game is already pending or a button is active here.",
            ephemeral=True
        )
        return

    await start_new_game_button(interaction.channel, "triples")
    await interaction.followup.send(
        "✅ Triples game button posted!",
        ephemeral=True
    )



@tree.command(
    name="leaderboard",
    description="Show the ELO leaderboard or stats for a specific user"
)
@discord.app_commands.describe(user="User to check in the leaderboard")
async def leaderboard_local(interaction: discord.Interaction, user: discord.User = None):
    # 1️⃣ Fetch all players ordered by rank descending
    res = await supabase.table("players").select("*").order("rank", desc=True).execute()
    if res.error or not res.data:
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
    description="Reset a user's stats (admin only)"
)
@app_commands.describe(user="User to reset")
async def resetstats(interaction: discord.Interaction, user: discord.User):
    # ✅ Check admin permissions
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "⛔ You must be an admin to use this.",
            ephemeral=True
        )
        return

    # ✅ Reset player in Supabase
    res = await supabase.table("players").upsert({
        "id": user.id,
        **default_template
    }).execute()

    if res.error:
        await interaction.response.send_message(
            f"⚠️ Failed to reset stats: {res.error.message}",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"✅ Stats for {user.display_name} have been reset.",
            ephemeral=True
        )


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

    res = await supabase.table("players").select("*").eq("id", str(target_user.id)).single().execute()

    if res.error and res.status_code != 406:
        await interaction.followup.send(f"⚠️ Error fetching stats: {res.error.message}", ephemeral=True)
        return

    player = res.data or default_template.copy()

    wins = player.get("wins", 0)
    losses = player.get("losses", 0)  # ✅ fixed typo here!
    draws = player.get("draws", 0)
    games = player.get("games_played", 0)
    trophies = player.get("trophies", 0)
    streak = player.get("current_streak", 0)
    best_streak = player.get("best_streak", 0)
    rank = player.get("rank", 1000)
    credits = player.get("credits", 1000)

    bets = await supabase.table("bets").select("*").eq("player_id", str(target_user.id)).order("id", desc=True).limit(5).execute()
    all_bets = await supabase.table("bets").select("id,won,payout,amount").eq("player_id", str(target_user.id)).execute()

    total_bets = len(all_bets.data or [])
    bets_won = sum(1 for b in all_bets.data if b.get("won") is True)
    bets_lost = sum(1 for b in all_bets.data if b.get("won") is False)
    net_gain = sum(b.get("payout", 0) - b.get("amount", 0) for b in all_bets.data if b.get("won") is not None)

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

    if bets.data:
        lines = []
        for b in bets.data:
            result = "Won ✅" if b.get("won") else "Lost ❌" if b.get("won") is False else "Pending ⏳"
            lines.append(f"{result} {b.get('amount')} on {b.get('choice')} (Payout: {b.get('payout')})")
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
    # ✅ Always defer first
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)

    try:
        if user:
            # ✅ Only clear this user from active players
            player_manager.deactivate(user.id)
            await interaction.followup.send(
                f"✅ Cleared active status for {user.display_name}.",
                ephemeral=True
            )
            return

        # ✅ Clear ALL pending games
        for key in pending_games:
            pending_games[key] = None

        # ✅ Clear all active players
        player_manager.clear()

        # ✅ Clear all start buttons
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
    update = { "id": str(user.id), field: value }
    res = await supabase.table("players").upsert(update).execute()

    if res.error:
        await interaction.response.send_message(
            f"❌ Error updating: {res.error.message}",
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
    await supabase.table("pending_games").delete().neq("game_type", "").execute()

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
    # ✅ Check admin
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "⛔ You don't have permission to use this command.",
            ephemeral=True
        )
        return

    # ✅ Fetch current credits
    query = await supabase.table("players").select("credits").eq("id", str(user.id)).single().execute()

    if query.error:
        current_credits = 0  # assume new
    else:
        current_credits = query.data.get("credits", 0)

    # ✅ Add amount
    new_credits = current_credits + amount

    # ✅ Upsert back to Supabase
    res = await supabase.table("players").upsert({
        "id": str(user.id),
        "credits": new_credits
    }).execute()

    if res.error:
        await interaction.response.send_message(
            f"❌ Error adding credits: {res.error.message}",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"✅ Added {amount} credits to {user.display_name}. Now has **{new_credits}** credits.",
        ephemeral=True
    )



@bot.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {bot.user}")

    pending = await load_pending_games()
    for pg in pending:
        channel = bot.get_channel(pg["channel_id"])
        if channel:
            await start_new_game_button(channel, pg["game_type"])


bot.run(os.getenv("DISCORD_BOT_TOKEN"))
