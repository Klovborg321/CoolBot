
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
bot.tournaments = {}
tree = bot.tree


IS_TEST_MODE = os.getenv("TEST_MODE", "1") == "1"

TEST_PLAYER_IDS = [
    970268488239317023,
    807840646764429342,
    701689044635091124
]

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
async def update_course_average_par(course_id: str):
    """
    Recalculate and update the avg_par for the given course_id.
    """
    # 1) Get all scores for this course
    res = await run_db(lambda: supabase
        .table("handicaps")
        .select("score")
        .eq("course_id", course_id)
        .execute()
    )
    scores = [row["score"] for row in res.data or []]

    if not scores:
        # If no scores exist, skip update.
        return None

    new_avg = round(sum(scores) / len(scores), 1)

    # 2) Update the course row
    await run_db(lambda: supabase
        .table("courses")
        .update({"avg_par": new_avg})
        .eq("id", course_id)
        .execute()
    )

    return new_avg


def format_page(self, guild):
    start = self.page * self.page_size
    end = start + self.page_size
    lines = []

    for i, entry in enumerate(self.entries[start:end], start=start + 1):
        if isinstance(entry, tuple):
            uid, stats = entry
        else:
            stats = entry
            uid = stats.get("id")

        # Use display name instead of mention so it's fixed length
        member = guild.get_member(int(uid))
        display = member.display_name if member else f"User {uid}"
        name = display[:18].ljust(18)

        rank = stats.get("rank", 1000)
        trophies = stats.get("trophies", 0)
        credits = stats.get("credits", 0)

        badge = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else ""

        line = f"#{i:>2} {name} | 🏆 {trophies:<3} | 💰 {credits:<4} | 📈 {rank} {badge}"
        lines.append(line)

    if not lines:
        lines = ["No entries found."]

    page_info = f"Page {self.page + 1} of {max(1, (len(self.entries) + self.page_size - 1) // self.page_size)}"
    return f"```{chr(10).join(lines)}\n\n{page_info}```"


def normalize_team(name):
    if isinstance(name, str):
        name = name.strip().upper()
        if name in ("A", "TEAM A"):
            return "A"
        if name in ("B", "TEAM B"):
            return "B"
    return name

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
async def save_pending_game(game_type, players, channel_id, max_players):
    await run_db(lambda: supabase.table("pending_games").upsert({
        "game_type": game_type,
        "players": players,
        "channel_id": channel_id,
        "max_players": max_players  # ✅ store it!
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

    if not res.data:  # If no player is found, return a default template
        # No row found → create one
        new_data = default_template.copy()
        new_data["id"] = str(user_id)
        await run_db(lambda: supabase.table("players").insert(new_data).execute())
        return new_data

    return res.data[0]  # Return the first player record found


def calculate_elo(elo1, elo2, result):
    expected = 1 / (1 + 10 ** ((elo2 - elo1) / 400))
    return elo1 + 32 * (result - expected)

def player_display(user_id, data):
    player = data.get(str(user_id), {"rank": 1000, "trophies": 0})
    return f"<@{user_id}> | Rank: {player['rank']} | Trophies: {player['trophies']}"

async def start_new_game_button(channel, game_type, max_players=None):
    key = (channel.id, game_type)

    # ✅ 1) Always clean up the old tracked button if it exists
    old = start_buttons.get(key)
    if old:
        try:
            await old.delete()
            print(f"🗑️ Deleted old start button for {game_type} in #{channel.name}")
        except discord.NotFound:
            print(f"⚠️ Old button already deleted for {game_type} in #{channel.name}")
        except Exception as e:
            print(f"⚠️ Could not delete old start button: {e}")

    # ✅ 2) Create and send the new button
    if game_type == "tournament":
        view = TournamentStartButtonView()
        msg = await channel.send("🏆 Click to start a **Tournament**:", view=view)
    else:
        view = GameJoinView(game_type, max_players)
        msg = await channel.send(f"🎮 Start a new {game_type} game:", view=view)

    # ✅ 3) Store the new one
    start_buttons[key] = msg

    print(f"✅ New start button posted for {game_type} in #{channel.name}")

    return msg



async def show_betting_phase(self):
    self.clear_items()
    self.add_item(BettingButtonDropdown(self))

    # ✅ Delay once to avoid "Unknown Channel" if thread is too fresh
    await asyncio.sleep(1)

    try:
        await self.update_message()
    except discord.NotFound:
        print("⚠️ Thread channel not ready yet, skipping initial update.")

    await asyncio.sleep(120)
    self.betting_closed = True
    self.clear_items()
    await self.update_message()


async def update_message(self, no_image=True):
    if self.message:
        embed = await self.build_embed(self.message.guild, no_image=no_image)
        await self.message.edit(embed=embed, view=self)

def fixed_width_name(name: str, width: int = 20) -> str:
    """Truncate or pad name to exactly `width` characters."""
    name = name.strip()
    if len(name) > width:
        return name[:width - 3] + "..."
    return name.ljust(width)


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
        # ✅ Block duplicate games of the same type
        if self.game_type in pending_games and pending_games[self.game_type]:
            await interaction.response.send_message(
                "A game of this type is already pending.", ephemeral=True)
            return

        # ✅ Block ANY other active game (cross-lobby)
        if player_manager.is_active(interaction.user.id):
            await interaction.response.send_message(
                "🚫 You are already in another game or have not voted yet.", ephemeral=True)
            return

        # ✅ OK! Activate and start the game
        player_manager.activate(interaction.user.id)

        # Pass max_players to the GameView initialization
        view = GameView(self.game_type, interaction.user.id, self.max_players)
        embed = await view.build_embed(interaction.guild, no_image=True)
        view.message = await interaction.channel.send(embed=embed, view=view)
        pending_games[self.game_type] = view  # Update pending game with the current view

        # Remove the "Start new game" button after the game has started
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


class BettingButton(discord.ui.Button):
    def __init__(self, game_view):
        super().__init__(label="Place Bet", style=discord.ButtonStyle.primary)
        self.game_view = game_view

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id in self.game_view.players:
            await interaction.response.send_message("Players cannot place bets.", ephemeral=True)
            return
        await interaction.response.send_modal(BetModal(self.game_view))

class BetModal(discord.ui.Modal, title="Place Your Bet"):
    def __init__(self, game_view, preselected=None):
        super().__init__()
        self.game_view = game_view

        self.bet_choice = discord.ui.TextInput(
            label="Choice (A/B/1/2)",
            placeholder="A, B, 1 or 2",
            max_length=1,
            default=preselected or ""
        )
        self.bet_amount = discord.ui.TextInput(
            label="Bet Amount",
            placeholder="Enter a positive number"
        )

        self.add_item(self.bet_choice)
        self.add_item(self.bet_amount)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            user_id = interaction.user.id
            choice = self.bet_choice.value.strip().upper()
            amount_raw = self.bet_amount.value.strip()

            # ✅ Validate amount
            try:
                amount = int(amount_raw)
                if amount <= 0:
                    raise ValueError()
            except ValueError:
                await interaction.response.send_message("❌ Invalid amount. Please enter a positive integer.", ephemeral=True)
                return

            # ✅ Validate choice
            valid_choices = {"A", "B", "1", "2"}
            if choice not in valid_choices:
                await interaction.response.send_message(f"❌ Invalid choice. Use one of: {', '.join(valid_choices)}.", ephemeral=True)
                return

            # ✅ Compute odds & payout safely
            odds = await self.game_view.get_odds(choice)
            payout = max(1, int(amount / odds)) if odds > 0 else amount

            # ✅ Atomic balance deduction
            success = await deduct_credits_atomic(user_id, amount)
            if not success:
                await interaction.response.send_message("❌ Not enough credits to place this bet.", ephemeral=True)
                return

            # ✅ Insert bet in DB
            await run_db(lambda: supabase
                .table("bets")
                .insert({
                    "player_id": str(user_id),
                    "game_id": self.game_view.message.id,
                    "choice": choice,
                    "amount": amount,
                    "payout": payout,
                    "won": None
                })
                .execute()
            )

            # ✅ Add to live bets
            await self.game_view.add_bet(user_id, interaction.user.display_name, amount, choice)

            # ✅ One guaranteed response
            await interaction.response.send_message(
                f"✅ Bet placed!\n• Choice: **{choice}**\n• Bet: **{amount}**\n• Odds: **{odds * 100:.1f}%**\n• Payout: **{payout}**",
                ephemeral=True
            )

        except Exception as e:
            # Failsafe: if interaction already used, fallback
            try:
                await interaction.followup.send(f"❌ Bet failed: {e}", ephemeral=True)
            except:
                pass



class BetDropdown(discord.ui.Select):
    def __init__(self, game_view):
        self.game_view = game_view
        self.options_built = False  # Lazy flag
        super().__init__(
            placeholder="Select who to bet on...",
            min_values=1,
            max_values=1,
            options=[]  # Will fill later
        )

    async def build_options(self):
        players = self.game_view.players or []
        game_type = self.game_view.game_type
        guild = self.game_view.message.guild if self.game_view.message else None

        options = []

        if game_type == "singles" and len(players) >= 2:
            ranks = [await get_player(p) for p in players]
            e1, e2 = [p.get("rank", 1000) for p in ranks]
            p1_odds = 1 / (1 + 10 ** ((e2 - e1) / 400))
            p2_odds = 1 - p1_odds

            for i, (player_id, odds) in enumerate(zip(players, [p1_odds, p2_odds]), start=1):
                member = guild.get_member(player_id) if guild else None
                name = member.display_name if member else f"Player {i}"
                name = fixed_width_name(name)
                options.append(discord.SelectOption(
                    label=f"{name} ({odds * 100:.1f}%)", value=str(i)
                ))

        elif game_type == "doubles" and len(players) >= 4:
            ranks = [await get_player(p) for p in players]
            team1 = sum([p.get("rank", 1000) for p in ranks[:2]]) / 2
            team2 = sum([p.get("rank", 1000) for p in ranks[2:]]) / 2
            a_odds = 1 / (1 + 10 ** ((team2 - team1) / 400))
            b_odds = 1 - a_odds

            options.extend([
                discord.SelectOption(label=f"Team A ({a_odds * 100:.1f}%)", value="A"),
                discord.SelectOption(label=f"Team B ({b_odds * 100:.1f}%)", value="B")
            ])

        elif game_type == "triples" and len(players) >= 3:
            ranks = [await get_player(p) for p in players]
            exp = [10 ** (p.get("rank", 1000) / 400) for p in ranks]
            total = sum(exp)
            odds = [v / total for v in exp]

            for i, (player_id, o) in enumerate(zip(players, odds), start=1):
                member = guild.get_member(player_id) if guild else None
                name = member.display_name if member else f"Player {i}"
                name = fixed_width_name(name)
                options.append(discord.SelectOption(
                    label=f"{name} ({o * 100:.1f}%)", value=str(i)
                ))

        # ✅ Always fallback option if empty
        if not options:
            options = [
                discord.SelectOption(label="⚠️ No valid choices", value="none")
            ]

        # ✅ Clear & replace safely
        self.options.clear()
        self.options.extend(options)
        self.options_built = True

    async def callback(self, interaction: discord.Interaction):
        if not self.options_built:
            await self.build_options()

        choice = self.values[0]

        if choice == "none":
            await interaction.response.send_message(
                "⚠️ No valid bet choices available.", ephemeral=True
            )
            return

        await interaction.response.send_modal(
            BetAmountModal(choice, self.game_view)
        )



class RoomView(discord.ui.View):
    def __init__(self, players, game_type, room_name, lobby_message=None, lobby_embed=None, game_view=None, course_name=None, course_id=None):
        super().__init__(timeout=None)
        self.players = players
        self.game_type = game_type
        self.room_name = room_name
        self.message = None  # thread message
        self.lobby_message = lobby_message
        self.lobby_embed = lobby_embed
        self.game_view = game_view

        # ✅ Store course_name robustly:
        self.course_name = course_name or getattr(game_view, "course_name", None)
        self.course_id = course_id or getattr(game_view, "course_id", None)

        self.votes = {}
        self.vote_timeout = None
        self.game_has_ended = False
        self.voting_closed = False
        self.add_item(GameEndedButton(self))

    async def build_room_embed(self, guild=None):
        guild = guild or self.guild or (self.message.guild if self.message else None)
        assert guild, "Guild is missing for RoomView!"

        embed = discord.Embed(
            title=f"🎮 {self.game_type.title()} Match Room",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow()
        )

        # ✅ 1️⃣ Show course name FIRST in description
        embed.description = f"🏌️ Course: **{self.course_name}**"

        # ✅ 2️⃣ Build detailed player lines
        lines = []
        for p in self.players:
            pdata = await get_player(p)
            rank = pdata.get("rank", 1000)
            trophies = pdata.get("trophies", 0)

            member = guild.get_member(p)
            name = member.display_name if member else f"ID {p}"
            lines.append(f"• **{name}** — Rank: {rank}, 🏆 {trophies}")

        # ✅ 3️⃣ Add Players field BELOW description
        embed.add_field(name="👥 Players", value="\n".join(lines), inline=False)

        # ✅ 4️⃣ Add status field
        embed.add_field(name="🎮 Status", value="Match in progress.", inline=False)

        # ✅ 5️⃣ Add course image if available
        if self.lobby_embed and self.lobby_embed.image:
            embed.set_image(url=self.lobby_embed.image.url)
        elif getattr(self, "course_image", None):
            embed.set_image(url=self.course_image)

        return embed


    def get_vote_options(self):
        if self.game_type in ("singles", "triples"):
            return self.players
        return ["Team A", "Team B"]

    async def build_lobby_end_embed(self, winner):
        embed = discord.Embed(
            title=f"{self.game_type.title()} Match",
            color=discord.Color.dark_gray()
        )

        lines = []
        for p in self.players:
            pdata = await get_player(p)
            rank = pdata.get('rank', 1000)
            trophies = pdata.get('trophies', 0)

            # ✅ Fully safe handicap lookup:
            hcp = "-"
            if self.course_name:
                res = await run_db(lambda: supabase
                    .table("handicaps")
                    .select("handicap")
                    .eq("player_id", str(p))
                    .eq("course_id", self.course_id)
                    .maybe_single()
                    .execute()
                )
                if res and getattr(res, "data", None):
                    hval = res.data.get("handicap")
                    hcp = round(hval, 1) if hval is not None else "-"

            lines.append(f"<@{p}> | Rank: {rank} | Trophies: {trophies} | 🎯 HCP: {hcp}")

        embed.description = "\n".join(lines)
        embed.add_field(name="🎮 Status", value="Game has ended.", inline=False)

        if winner == "draw":
            embed.add_field(name="🏁 Result", value="🤝 It's a draw!", inline=False)
        elif isinstance(winner, int):
            member = self.message.guild.get_member(winner)
            name = member.display_name if member else f"User {winner}"
            name = fixed_width_name(name)
            embed.add_field(name="🏁 Winner", value=f"🎉 {name}", inline=False)
        elif winner in ("Team A", "Team B"):
            embed.add_field(name="🏁 Winner", value=f"🎉 {winner}", inline=False)

        # ✅ Use lobby image if it exists:
        if self.lobby_embed and self.lobby_embed.image:
            embed.set_image(url=self.lobby_embed.image.url)

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

        # ✅ 1️⃣ Update player stats & handle draw
        if winner == "draw":
            for p in self.players:
                pdata = await get_player(p)
                pdata["draws"] += 1
                pdata["games_played"] += 1
                pdata["current_streak"] = 0
                await save_player(p, pdata)

            for uid, uname, amount, choice in self.game_view.bets:
                await add_credits_internal(uid, amount)
                await run_db(lambda: supabase
                    .table("bets")
                    .update({"won": None})
                    .eq("player_id", uid)
                    .eq("game_id", self.game_view.message.id)
                    .eq("choice", choice)
                    .execute()
                )
                print(f"↩️ Refunded {amount} to {uname} (DRAW)")

            # ✅ Thread embed uses RoomView version:
            embed = await self.build_lobby_end_embed(winner)
            await self.message.edit(embed=embed, view=None)

            # ✅ Lobby embed uses GameView version, no image:
            if self.lobby_message:
                lobby_embed = await self.game_view.build_embed(self.lobby_message.guild, winner=winner, no_image=True)
                await self.lobby_message.edit(embed=lobby_embed, view=None)

            await self.message.channel.send("🤝 Voting ended in a **draw** — all bets refunded.")
            await self.message.channel.edit(archived=True)
            return

        # ✅ 2️⃣ Win case: stats update
        normalized_winner = normalize_team(winner) if self.game_type == "doubles" else winner

        for p in self.players:
            pdata = await get_player(p)
            pdata["games_played"] += 1

            if self.game_type == "singles":
                is_winner = (winner == p)
            elif self.game_type == "doubles":
                is_winner = p in self.players[:2] if normalized_winner == "A" else p in self.players[2:]
            elif self.game_type == "triples":
                is_winner = (winner == p)
            else:
                is_winner = False

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

        # ✅ 3️⃣ Resolve bets
        if self.game_view is None:
            # Tournament match: skip bet resolution
            pass  # or log something
        else:
            for uid, uname, amount, choice in self.game_view.bets:
                won = False
                if self.game_type == "singles":
                    won = (choice == "1" and self.players[0] == winner) or \
                          (choice == "2" and self.players[1] == winner)
                elif self.game_type == "doubles":
                    won = normalize_team(choice) == normalize_team(winner)
                elif self.game_type == "triples":
                    try:
                        idx = int(choice) - 1
                        won = self.players[idx] == winner
                    except:
                        won = False

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
                    payout = profit + amount
                    await add_credits_internal(uid, payout)
                    print(f"💰 {uname} won! Payout: {payout} (bet {amount}, profit {profit})")
                else:
                    print(f"❌ {uname} lost {amount} (stake was upfront)")

        # ✅ 4️⃣ Final result:
        winner_name = winner
        if isinstance(winner, int):
            member = self.message.guild.get_member(winner)
            winner_name = member.display_name if member else f"User {winner}"

        embed = await self.build_lobby_end_embed(winner)
        await self.message.edit(embed=embed, view=None)

        if self.lobby_message:
            lobby_embed = await self.game_view.build_embed(self.lobby_message.guild, winner=winner, no_image=True)
            await self.lobby_message.edit(embed=lobby_embed, view=None)

        await self.message.channel.send(f"🏁 Voting ended. Winner: **{winner_name}**")
        await asyncio.sleep(30)
        await self.message.channel.edit(archived=True)

        if self.on_tournament_complete:
            if isinstance(winner, int):
                await self.on_tournament_complete(winner)


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
        if IS_TEST_MODE or len(self.view_obj.votes) == len(self.view_obj.players):
            await self.view_obj.finalize_game()


class TournamentStartButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Start Tournament", style=discord.ButtonStyle.primary)
    async def start_tournament(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Remove previous start button message, if any
        key = (interaction.channel.id, "tournament")
        old = start_buttons.get(key)
        if old:
            try:
                await old.delete()
            except Exception:
                pass
            start_buttons[key] = None

        # ✅ ALWAYS pass parent_channel and creator — no more missing args!
        modal = PlayerCountModal(
            parent_channel=interaction.channel,
            creator=interaction.user
        )
        await interaction.response.send_modal(modal)


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
        self.course_image = None
        self.on_tournament_complete = None

        # ✅ static Leave button:
        self.add_item(LeaveGameButton(self))

    @discord.ui.button(label="Join Game", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id in self.players:
            await interaction.response.send_message(
                "✅ You have already joined this game.", ephemeral=True)
            return

        if len(self.players) >= self.max_players:
            await interaction.response.send_message(
                "🚫 This game is already full.", ephemeral=True)
            return

        if player_manager.is_active(interaction.user.id):
            await interaction.response.send_message(
                "🚫 You are already in another active game or must finish voting first.",
                ephemeral=True)
            return

        player_manager.activate(interaction.user.id)
        self.players.append(interaction.user.id)
        await interaction.response.defer()

        # ✅ Update lobby with new player
        await self.update_message()

        if len(self.players) == self.max_players:
            await self.game_full(interaction)


    async def abandon_game(self, reason):
        global pending_game
        pending_games[self.game_type] = None

        for p in self.players:
            player_manager.deactivate(p)

        embed = discord.Embed(
            title="❌ Game Abandoned",
            description=reason,
            color=discord.Color.red()
        )
        await self.message.edit(embed=embed, view=None)
        await start_new_game_button(self.message.channel, self.game_type, self.max_players)

    async def abandon_if_not_filled(self):
        timeout_duration = 1000  # seconds
        elapsed = 0
        while len(self.players) < self.max_players and not self.betting_closed and elapsed < timeout_duration:
            await asyncio.sleep(30)
            elapsed += 30

        if len(self.players) < self.max_players and not self.betting_closed:
            await self.abandon_game("⏰ Game timed out due to inactivity.")
            await clear_pending_game(self.game_type)

    async def build_embed(self, guild=None, winner=None, no_image=True):
        # ✅ Compute the title first
        if self.game_type == "tournament":
            title = "🏆 Tournament Lobby"
        else:
            title = f"🎮 {self.game_type.title()} Match Lobby"

        # ✅ Then build the embed using that variable
        embed = discord.Embed(
            title=title,
            description="Awaiting players for a new match..." if not winner else "",
            color=discord.Color.orange() if not winner else discord.Color.dark_gray(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_author(
            name="LEAGUE OF EXTRAORDINARY MISFITS",
            icon_url="https://cdn.discordapp.com/attachments/1378860910310854666/1382601173932183695/LOGO_2.webp"
        )

        if not no_image and getattr(self, "course_image", None):
            embed.set_image(url=self.course_image)

        ranks = []
        handicaps = []
        for p in self.players:
            pdata = await get_player(p)
            ranks.append(pdata.get("rank", 1000))
            if not no_image and getattr(self, "course_name", None):
                res = await run_db(lambda: supabase
                    .table("handicaps")
                    .select("handicap")
                    .eq("player_id", str(p))
                    .eq("course_id", self.course_id)
                    .maybe_single()
                    .execute()
                )
                hcp = round(res.data["handicap"], 1) if (res and res.data and "handicap" in res.data) else "-"
            else:
                hcp = None
            handicaps.append(hcp)

        game_full = len(self.players) == self.max_players
        odds = []

        if self.game_type == "doubles" and game_full:
            e1 = sum(ranks[:2]) / 2
            e2 = sum(ranks[2:]) / 2
            odds_a = 1 / (1 + 10 ** ((e2 - e1) / 400))
            odds_b = 1 - odds_a
        elif self.game_type == "triples" and game_full:
            exp = [10 ** (r / 400) for r in ranks]
            total = sum(exp)
            odds = [v / total for v in exp]

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
                raw_name = member.display_name if member else f"Player {idx + 1}"
                name = f"**{fixed_width_name(raw_name, 20)}**"
                rank = ranks[idx]
                hcp_txt = f" 🎯 HCP: {handicaps[idx]}" if handicaps[idx] is not None else ""

                if self.game_type == "singles" and game_full:
                    e1, e2 = ranks
                    o1 = 1 / (1 + 10 ** ((e2 - e1) / 400))
                    player_odds = o1 if idx == 0 else 1 - o1
                    line = f"● Player {idx + 1}: {name} 🏆 ({rank}) • {player_odds * 100:.1f}%{hcp_txt}"
                elif self.game_type == "triples" and game_full:
                    line = f"● Player {idx + 1}: {name} 🏆 ({rank}) • {odds[idx] * 100:.1f}%{hcp_txt}"
                else:
                    line = f"● Player {idx + 1}: {name} 🏆 ({rank}){hcp_txt}"
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
        embed.add_field(name="\u200b", value="\u200b", inline=False)

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

        if winner == "draw":
            embed.set_footer(text="🎮 Game has ended. Result: 🤝 Draw")
        elif isinstance(winner, int):
            member = guild.get_member(winner) if guild else None
            winner_name = member.display_name if member else f"User {winner}"
            embed.set_footer(text=f"🎮 Game has ended. Winner: {winner_name}")
        elif winner in ("Team A", "Team B"):
            embed.set_footer(text=f"🎮 Game has ended. Winner: {winner}")

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

    async def show_betting_phase(self):
        self.clear_items()
        self.add_item(BettingButtonDropdown(self))
        await self.update_message()
        await asyncio.sleep(120)
        self.betting_closed = True
        self.clear_items()
        await self.update_message()

    async def game_full(self, interaction):
        global pending_games

        # ✅ Stop abandon timer
        if self.abandon_task:
            self.abandon_task.cancel()

        # ✅ Mark no more pending game for this type
        pending_games[self.game_type] = None
        await save_pending_game(self.game_type, self.players, self.message.channel.id, self.max_players)

        await start_new_game_button(self.message.channel, self.game_type, self.max_players)

        # ✅ Select random course from DB
        res = await run_db(lambda: supabase.table("courses").select("id", "name", "image_url").execute())
        chosen = random.choice(res.data or [{}])
        self.course_id = chosen.get("id")
        self.course_name = chosen.get("name", "Unknown")
        self.course_image = chosen.get("image_url", "")

        # ✅ Create unique thread
        room_name = await room_name_generator.get_unique_word()
        thread = await interaction.channel.create_thread(name=room_name)

        # ✅ Thread embed WITH image
        thread_embed = await self.build_embed(interaction.guild, no_image=False)
        thread_embed.title = f"Game Room: {room_name}"
        thread_embed.description = f"Course: {self.course_name}"

        room_view = RoomView(
            players=self.players,
            game_type=self.game_type,
            room_name=room_name,
            lobby_message=self.message,
            lobby_embed=thread_embed,
            game_view=self,
            course_name=self.course_name,
            course_id=self.course_id
        )
        room_view.original_embed = thread_embed.copy()

        mentions = " ".join(f"<@{p}>" for p in self.players)
        thread_msg = await thread.send(content=f"{mentions}\nMatch started!", embed=thread_embed, view=room_view)
        room_view.message = thread_msg

        # ✅ MAIN LOBBY embed — NO image, mark thread info
        lobby_embed = await self.build_embed(interaction.guild, no_image=True)
        lobby_embed.title = f"{self.game_type.title()} Match Created!"
        lobby_embed.description = f"A match has been created in thread: {thread.mention}"
        lobby_embed.color = discord.Color.orange()

        # ✅ Replace Join button with Bet button:
        self.clear_items()  # remove Join & Leave
        self.add_item(BettingButtonDropdown(self))

        await self.message.edit(embed=lobby_embed, view=self)

        # ✅ Auto close betting after 2 mins
        await asyncio.sleep(120)
        self.betting_closed = True

        self.clear_items()
        await self.message.edit(embed=lobby_embed, view=self)


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
        super().__init__(timeout=120)
        self.dropdown = BetDropdown(game_view)
        self.add_item(self.dropdown)

    async def prepare(self):
        await self.dropdown.build_options()


class LeaderboardView(discord.ui.View):
    def __init__(self, entries, page_size=10, title="🏆 Leaderboard"):
        """
        entries: list of (id, stats_dict) OR list of stats_dicts with 'id'
        """
        super().__init__(timeout=120)
        self.entries = entries
        self.page_size = page_size
        self.page = 0
        self.title = title
        self.message = None
        self.update_buttons()

    def update_buttons(self):
        self.clear_items()
        if self.page > 0:
            self.add_item(self.PreviousButton(self))
        if (self.page + 1) * self.page_size < len(self.entries):
            self.add_item(self.NextButton(self))

    def format_page(self, guild):
        start = self.page * self.page_size
        end = start + self.page_size
        lines = []

        for i, entry in enumerate(self.entries[start:end], start=start + 1):
            # Support both (id, stats) or plain dicts with 'id'
            if isinstance(entry, tuple):
                uid, stats = entry
            else:
                stats = entry
                uid = stats.get("id")

            member = guild.get_member(int(uid))
            display = member.display_name if member else f"User {uid}"
            name = display[:18].ljust(18)  # ✅ force exactly 18 chars

            rank = stats.get("rank", 1000)
            trophies = stats.get("trophies", 0)
            credits = stats.get("credits", 0)

            badge = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else ""

            line = f"#{i:>2} {name} | 🏆 {trophies:<3} | 💰 {credits:<4} | 📈 {rank} {badge}"
            lines.append(line)

        if not lines:
            lines = ["No entries found."]

        page_info = f"Page {self.page + 1} of {max(1, (len(self.entries) + self.page_size - 1) // self.page_size)}"
        return f"```{chr(10).join(lines)}\n\n{page_info}```"  # ✅ wrap in code block!

    async def update(self):
        self.update_buttons()
        embed = discord.Embed(
            title=self.title,
            description=self.format_page(self.message.guild),
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
            max_pages = (len(self.view_obj.entries) - 1) // self.view_obj.page_size
            self.view_obj.page = min(max_pages, self.view_obj.page + 1)
            await self.view_obj.update()
            await interaction.response.defer()



class PaginatedCourseView(discord.ui.View):
    def __init__(self, courses, per_page=25):
        super().__init__(timeout=120)
        self.courses = courses
        self.per_page = per_page
        self.page = 0
        self.message = None
        self.update_children()

    def has_options(self):
        """True if there is at least one course total"""
        return len(self.courses) > 0

    def update_children(self):
        self.clear_items()
        start = self.page * self.per_page
        end = start + self.per_page
        page_courses = self.courses[start:end]

        if page_courses:
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


class SubmitScoreModal(discord.ui.Modal, title="Submit Best Score"):
    def __init__(self, course_name: str, course_id: str):
        super().__init__()

        short_name = (course_name[:30] + "...") if len(course_name) > 30 else course_name

        self.best_score = discord.ui.TextInput(
            label=f"Best score for {short_name}",
            placeholder="e.g. 44",
            required=True
        )

        self.course_id = course_id
        self.course_name = course_name

        self.add_item(self.best_score)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            score = int(self.best_score.value.strip())
        except ValueError:
            await interaction.response.send_message("❌ Invalid score.", ephemeral=True)
            return

        # 1️⃣ Insert raw score
        await run_db(lambda: supabase
            .table("handicaps")
            .upsert({
                "player_id": str(interaction.user.id),
                "course_id": self.course_id,
                "score": score
            })
            .execute()
        )

        # 2️⃣ Recompute avg_par
        new_avg = await update_course_average_par(self.course_id)

        # 3️⃣ Compute correct handicap
        handicap = score - new_avg

        # 4️⃣ Update the same row
        await run_db(lambda: supabase
            .table("handicaps")
            .update({"handicap": handicap})
            .eq("player_id", str(interaction.user.id))
            .eq("course_id", self.course_id)
            .execute()
        )

        await interaction.response.send_message(
            f"✅ Saved score: **{score}**\n"
            f"🎯 Handicap vs avg: **{handicap:+.1f}**\n"
            f"📊 Updated course avg: **{new_avg:.1f}**",
            ephemeral=True
        )


class CourseSelect(discord.ui.Select):
    def __init__(self, courses, callback_fn):
        options = [
            discord.SelectOption(label=c["name"], value=str(c["id"]))
            for c in courses
        ]
        super().__init__(placeholder="Select a course...", options=options)
        self.callback_fn = callback_fn

    async def callback(self, interaction: discord.Interaction):
        selected_id = self.values[0]
        await self.callback_fn(interaction, selected_id)


class CourseSelectView(discord.ui.View):
    def __init__(self, courses, callback_fn):
        super().__init__(timeout=120)
        self.add_item(CourseSelect(courses, callback_fn))

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


class AddCourseModal(discord.ui.Modal, title="Add New Course (Easy & Hard)"):
    def __init__(self):
        super().__init__()

        self.name = discord.ui.TextInput(
            label="Base Course Name",
            placeholder="e.g. Pebble Beach"
        )
        self.image_url = discord.ui.TextInput(
            label="Image URL",
            placeholder="https://..."
        )

        # Easy version rating only
        self.easy_rating = discord.ui.TextInput(
            label="Easy Course Par",
            placeholder="e.g. 60",
            required=False
        )

        # Hard version rating only
        self.hard_rating = discord.ui.TextInput(
            label="Hard Course Par",
            placeholder="e.g. 64",
            required=False
        )

        self.add_item(self.name)
        self.add_item(self.image_url)
        self.add_item(self.easy_rating)
        self.add_item(self.hard_rating)

    async def on_submit(self, interaction: discord.Interaction):
        base_name = self.name.value.strip()
        image_url = self.image_url.value.strip()

        # Parse easy rating
        try:
            easy_rating = float(self.easy_rating.value.strip()) if self.easy_rating.value.strip() else None
        except ValueError:
            return await interaction.response.send_message(
                "❌ Invalid Easy Course Rating. Must be a number.",
                ephemeral=True
            )

        # Parse hard rating
        try:
            hard_rating = float(self.hard_rating.value.strip()) if self.hard_rating.value.strip() else None
        except ValueError:
            return await interaction.response.send_message(
                "❌ Invalid Hard Course Rating. Must be a number.",
                ephemeral=True
            )

        # Build both records — no slope_rating
        records = []

        easy = {
            "name": f"{base_name} Easy",
            "image_url": image_url
        }
        if easy_rating is not None:
            easy["course_par"] = easy_rating

        hard = {
            "name": f"{base_name} Hard",
            "image_url": image_url
        }
        if hard_rating is not None:
            hard["course_par"] = hard_rating

        records.append(easy)
        records.append(hard)

        # Insert both at once
        res = await run_db(lambda: supabase.table("courses").insert(records).execute())

        if hasattr(res, "status_code") and res.status_code not in (200, 201):
            await interaction.response.send_message(
                f"❌ Failed to add courses: {res}",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"✅ Added **{base_name} Easy** and **{base_name} Hard** with ratings!",
            ephemeral=True
        )



class SetCourseRatingModal(discord.ui.Modal, title="Set Course Par"):
    def __init__(self, course):
        super().__init__()
        self.course = course

        self.course_par = discord.ui.TextInput(
            label="Course Par",
            placeholder="e.g. 62",
            default=str(course.get("course_par") or "60.0")
        )
        self.avg_par = discord.ui.TextInput(
        label="Average Par",
        placeholder="e.g. 43",
        default=str(course.get("avg_par") or "55.0")
)
        self.add_item(self.course_par)
        self.add_item(self.avg_par)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            course_par = float(self.course_par.value)
            avg_par = float(self.avg_par.value)
        except ValueError:
            await interaction.response.send_message(
                "❌ Invalid numbers.", ephemeral=True
            )
            return

        await run_db(lambda: supabase
            .table("courses")
            .update({"course_par": course_par, "avg_par": avg_par})
            .eq("id", self.course["id"])
            .execute()
        )

        await interaction.response.send_message(
            f"✅ Updated **{self.course['name']}**:\n"
            f"• Course Par: **{course_par}**\n"
            f"• Average Par: **{avg_par}**",
            ephemeral=True
        )


######################################
# ✅ FINAL TOURNAMENT MODULE
######################################

class TournamentManager:
    def __init__(self, creator, max_players=8):
        self.creator = creator
        self.players = [creator]
        self.max_players = max_players

        self.message = None  # the lobby message in parent channel
        self.parent_channel = None  # base text channel for threads
        self.main_thread = None  # main bracket thread

        self.current_matches = []
        self.winners = []
        self.round_players = []

        self.abandon_task = asyncio.create_task(self.abandon_if_not_filled())

    async def add_player(self, user):
        if user.id in self.players or len(self.players) >= self.max_players:
            return False
        self.players.append(user.id)
        return True

    async def abandon_if_not_filled(self):
        await asyncio.sleep(300)
        if len(self.players) < self.max_players:
            embed = discord.Embed(
                title="❌ Tournament Abandoned",
                description="Not enough players joined in time.",
                color=discord.Color.red()
            )
            if self.message:
                await self.message.edit(embed=embed, view=None)
            await start_new_game_button(self.parent_channel, "tournament")

    async def start_bracket(self, interaction):
        self.parent_channel = interaction.channel
        self.round_players = self.players.copy()
        random.shuffle(self.round_players)

        # ✅ Create main bracket thread
        self.main_thread = await self.parent_channel.create_thread(
            name=f"Tournament-{random.randint(1000, 9999)}",
            type=discord.ChannelType.public_thread
        )
        await self.main_thread.send(
            f"🏆 Tournament started with {len(self.players)} players!"
        )

        await self.run_round(interaction.guild)

    async def run_round(self, guild):
        players = self.round_players.copy()
        random.shuffle(players)

        self.current_matches = []
        self.winners = []
        self.next_round_players = []

        # ✅ Pick ONE course for this round
        res = await run_db(lambda: supabase.table("courses").select("*").execute())
        chosen = random.choice(res.data or [{}])
        course_id = chosen.get("id")
        course_name = chosen.get("name", "Unknown")
        course_image = chosen.get("image_url", "")

        await self.main_thread.send(
            f"🏌️ New round starting on course: **{course_name}**"
        )

        # ✅ Create pairs of matches
        for i in range(0, len(players), 2):
            if i + 1 < len(players):
                p1 = players[i]
                p2 = players[i + 1]

                room_name = await room_name_generator.get_unique_word()

                match_thread = await self.parent_channel.create_thread(
                    name=f"Match-{room_name}",
                    type=discord.ChannelType.public_thread
                )

                room_view = RoomView(
                    players=[p1, p2],
                    game_type="singles",
                    room_name=room_name,
                    course_name=course_name,
                    course_id=course_id
                )
                room_view.parent_thread = self.main_thread
                room_view.course_image = course_image
                room_view.guild = guild
                room_view.on_tournament_complete = self.match_complete

                embed = await room_view.build_room_embed()
                embed.title = f"Room: {room_name}"
                embed.description = f"Course: {course_name}"
                room_view.lobby_embed = embed

                mentions = f"<@{p1}> <@{p2}>"

                msg = await match_thread.send(
                    content=f"{mentions}\n🏆 This match is part of {self.main_thread.mention}!",
                    embed=embed,
                    view=room_view
                )
                room_view.message = msg

                await self.main_thread.send(
                    f"📣 New match: {match_thread.mention} — {mentions}"
                )

                self.current_matches.append(room_view)

            else:
                # odd player advances automatically
                await self.main_thread.send(
                    f"✅ <@{players[i]}> advances automatically!"
                )
                self.next_round_players.append(players[i])

    async def match_complete(self, winner_id):
        self.winners.append(winner_id)
        self.next_round_players.append(winner_id)

        # ✅ Update MAIN LOBBY embed too:
        embed = discord.Embed(
            title="🏆 Tournament Complete",
            description=f"**Champion:** <@{champ}>",
            color=discord.Color.gold()
        )

        expected = len(self.current_matches)
        if len(self.winners) >= expected:
            if len(self.next_round_players) == 1:
                await self.main_thread.send(
                    f"🏆 Champion: <@{self.next_round_players[0]}> 🎉"
                )
                await start_new_game_button(self.parent_channel, "tournament")
            else:
                self.round_players = self.next_round_players.copy()
                await self.main_thread.send(
                    f"➡️ Next round with {len(self.round_players)} players..."
                )
                await self.run_round(self.main_thread.guild)



class TournamentLobbyView(discord.ui.View):
    def __init__(self, manager):
        super().__init__(timeout=None)
        self.manager = manager

    @discord.ui.button(label="Join Tournament", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        success = await self.manager.add_player(interaction.user)
        if not success:
            await interaction.response.send_message(
                "You already joined or the tournament is full.", ephemeral=True)
            return

        # ✅ Again: use dummy GameView to update embed with same layout
        #dummy = GameView("tournament", interaction.user.id, 2)
        #dummy.players = self.manager.players.copy()
        #dummy.max_players = self.manager.max_players

        #embed = await dummy.build_embed(interaction.guild, no_image=True)

        # With this:
        # ✅ Use real lobby embed, not fake GameView
        players_lines = []
        for pid in self.manager.players:
            member = interaction.guild.get_member(pid)
            display = member.display_name if member else f"ID {pid}"
            players_lines.append(f"• {display}")

        embed = discord.Embed(
            title="🏆 Tournament Lobby",
            description="\n".join(players_lines),
            color=discord.Color.gold()
        )
        embed.add_field(name="Max Players", value=f"{self.manager.max_players}", inline=False)
        embed.add_field(name="Status", value="Waiting for more players...", inline=False)

        await self.manager.message.edit(embed=embed, view=self)
        await interaction.response.send_message("✅ Joined the tournament!", ephemeral=True)

        # ✅ When lobby is full, lock & start
        if len(self.manager.players) == self.manager.max_players:
            self.clear_items()
            await self.manager.message.edit(view=None)
            if self.manager.abandon_task:
                self.manager.abandon_task.cancel()
            await self.manager.start_bracket(interaction)


class PlayerCountModal(discord.ui.Modal, title="Select Tournament Size"):
    def __init__(self, parent_channel, creator):
        super().__init__()
        self.parent_channel = parent_channel
        self.creator = creator  # the user who started it

        self.player_count = discord.ui.TextInput(
            label="Number of players (even number)",
            placeholder="E.g. 4, 8, 16",
            required=True
        )
        self.add_item(self.player_count)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            count = int(self.player_count.value.strip())
            if count % 2 != 0 or count < 2:
                raise ValueError()
        except ValueError:
            await interaction.response.send_message(
                "❌ Please enter an **even number** ≥ 2.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        manager = TournamentManager(creator=self.creator.id, max_players=count)
        manager.parent_channel = self.parent_channel
    
        # ✅ Register manager in bot.tournaments properly:
        interaction.client.tournaments[self.parent_channel.id] = manager

        if IS_TEST_MODE:
            for pid in TEST_PLAYER_IDS:
                if len(manager.players) < manager.max_players and pid not in manager.players:
                    manager.players.append(pid)

        dummy = GameView("tournament", self.creator.id, 2)
        dummy.players = manager.players.copy()
        dummy.max_players = manager.max_players

        embed = await dummy.build_embed(interaction.guild, no_image=True)
        view = TournamentLobbyView(manager)
        manager.message = await interaction.channel.send(embed=embed, view=view)

        if len(manager.players) == manager.max_players:
            view.clear_items()
            await manager.message.edit(view=None)
            if manager.abandon_task:
                manager.abandon_task.cancel()
            await manager.start_bracket(interaction)

        await interaction.followup.send(
            f"✅ Tournament lobby created for **{count} players!**",
            ephemeral=True
        )



@bot.tree.command(name="init_tournament")
async def init_tournament(interaction: discord.Interaction):
    # Always pass parent_channel & creator to modal
    modal = PlayerCountModal(parent_channel=interaction.channel, creator=interaction.user)
    await interaction.response.send_modal(modal)


@tree.command(name="set_user_handicap")
async def set_user_handicap(interaction: discord.Interaction):
    """Update your best score for a course"""

    # ✅ 1) Always defer immediately!
    await interaction.response.defer(ephemeral=True)

    # ✅ 2) Get all courses
    res = await run_db(lambda: supabase.table("courses").select("*").execute())
    courses = res.data or []

    if not courses:
        await interaction.followup.send("❌ No courses found.", ephemeral=True)
        return

    # ✅ 3) Build paginated view
    view = PaginatedCourseView(courses)
    msg = await interaction.followup.send(
        "Pick a course to set your best score:",
        view=view,
        ephemeral=True
    )
    view.message = msg  # ✅ so view knows where to edit pages



@tree.command(name="init_singles")
async def init_singles(interaction: discord.Interaction):
    """Creates a singles game lobby with the start button"""

    print("[init_singles] Defer interaction...")
    await interaction.response.defer(ephemeral=True)

    print("[init_singles] Checking for existing game or button...")
    if pending_games.get("singles") or any(k[0] == interaction.channel.id for k in start_buttons):
        print("[init_singles] Found existing game/button, sending followup...")
        await interaction.followup.send(
            "⚠️ A singles game is already pending or a button is active here.",
            ephemeral=True
        )
        return

    max_players = 2

    print("[init_singles] Calling start_new_game_button...")
    # ✅ Ensure this never takes 3+ seconds; if it might, break it up:
    await start_new_game_button(interaction.channel, "singles", max_players=max_players)

    print("[init_singles] Sending success followup...")
    await interaction.followup.send(
        "✅ Singles game button posted and ready for players to join!",
        ephemeral=True
    )


@tree.command(name="init_doubles")
async def init_doubles(interaction: discord.Interaction):
    """Creates a doubles game lobby with the start button"""

    try:
        # ✅ 1️⃣ IMMEDIATELY defer to get 15 min
        await interaction.response.defer(ephemeral=True)
    except discord.errors.NotFound:
        # ⚠️ If it's already expired, just exit — don't crash
        return

    # ✅ 2️⃣ Do checks
    if pending_games.get("doubles") or any(
        k[0] == interaction.channel.id for k in start_buttons
    ):
        await interaction.followup.send(
            "⚠️ A doubles game is already pending or a button is active here.",
            ephemeral=True
        )
        return

    # ✅ 3️⃣ Create the start button safely
    try:
        await start_new_game_button(interaction.channel, "doubles", max_players=4)
    except Exception as e:
        await interaction.followup.send(
            f"❌ Failed to create start button: {e}",
            ephemeral=True
        )
        return

    # ✅ 4️⃣ Confirm
    await interaction.followup.send(
        "✅ Doubles game button posted and ready for players to join!",
        ephemeral=True
    )


@tree.command(name="init_triples")
async def init_triples(interaction: discord.Interaction):
    """Creates a triples game lobby with the start button"""

    # Defer the interaction immediately to avoid timeout
    await interaction.response.defer(ephemeral=True)

    # Fast check if a game is already pending
    if pending_games.get("triples") or any(k[0] == interaction.channel.id for k in start_buttons):
        await interaction.followup.send(
            "⚠️ A triples game is already pending or a button is active here.",
            ephemeral=True
        )
        return

    # Set max_players for triples game
    max_players = 3

    # Create the button
    await start_new_game_button(interaction.channel, "triples", max_players=max_players)

    # Send confirmation to the user
    await interaction.followup.send(
        "✅ Triples game button posted and ready for players to join!",
        ephemeral=True
    )

@tree.command(
    name="leaderboard",
    description="Show the paginated leaderboard"
)
async def leaderboard_local(interaction: discord.Interaction):
    # 1️⃣ Fetch all players sorted by rank descending
    res = await run_db(
        lambda: supabase
        .table("players")
        .select("*")
        .order("rank", desc=True)
        .execute()
    )

    if not res.data:
        await interaction.response.send_message(
            "📭 No players found.",
            ephemeral=True
        )
        return

    # 2️⃣ Format as (id, stats) tuples
    entries = [(row["id"], row) for row in res.data]

    # 3️⃣ Create paginated view
    view = LeaderboardView(entries, page_size=10, title="🏆 Leaderboard")

    # 4️⃣ Send first page
    embed = discord.Embed(
        title=view.title,
        description=view.format_page(interaction.guild),
        color=discord.Color.gold()
    )
    await interaction.response.send_message(embed=embed, view=view)

    # 5️⃣ Bind view.message for updates
    view.message = await interaction.original_response()



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

    # Fetch player base stats
    res = await run_db(
        lambda: supabase.table("players").select("*").eq("id", str(target_user.id)).single().execute()
    )
    player = res.data or default_template.copy()

    rank = player.get("rank", 1000)
    trophies = player.get("trophies", 0)
    credits = player.get("credits", 1000)
    games = player.get("games_played", 0)
    wins = player.get("wins", 0)
    losses = player.get("losses", 0)
    draws = player.get("draws", 0)
    streak = player.get("current_streak", 0)
    best_streak = player.get("best_streak", 0)

    # Bets stats
    bets = await run_db(
        lambda: supabase.table("bets").select("id,won,payout,amount,choice").eq("player_id", str(target_user.id)).order("id", desc=True).limit(5).execute()
    )
    all_bets = await run_db(
        lambda: supabase.table("bets").select("won,payout,amount").eq("player_id", str(target_user.id)).execute()
    )

    total_bets = len(all_bets.data or [])
    bets_won = sum(1 for b in all_bets.data if b.get("won") is True)
    bets_lost = sum(1 for b in all_bets.data if b.get("won") is False)
    net_gain = sum(b.get("payout", 0) - b.get("amount", 0) for b in all_bets.data if b.get("won") is not None)

    # Build stats block
    stats_lines = [
        f"{'📈 Rank':<20}: {rank}",
        f"{'🏆 Trophies':<20}: {trophies}",
        f"{'💰 Credits':<20}: {credits}",
        "",
        f"{'🎮 Games Played':<20}: {games}",
        f"{'✅ Wins':<20}: {wins}",
        f"{'❌ Losses':<20}: {losses}",
        f"{'➖ Draws':<20}: {draws}",
        f"{'🔥 Current Streak':<20}: {streak}",
        f"{'🏅 Best Streak':<20}: {best_streak}",
        "",
        f"{'🪙 Total Bets':<20}: {total_bets}",
        f"{'✅ Bets Won':<20}: {bets_won}",
        f"{'❌ Bets Lost':<20}: {bets_lost}",
        f"{'💸 Net Gain/Loss':<20}: {net_gain:+}",
    ]

    embed = discord.Embed(
        title=f"📊 Stats for {target_user.display_name}",
        description="```" + "\n".join(stats_lines) + "```",
        color=discord.Color.blue()
    )

    # Add recent bets block, also monospaced
    if bets.data:
        recent_lines = []
        for b in bets.data:
            won = b.get("won")
            choice = b.get("choice", "?")
            amount = b.get("amount", 0)
            payout = b.get("payout", 0)
            if won is True:
                line = f"✅ Won  {amount:<5} on {choice:<4} → Payout {payout}"
            elif won is False:
                line = f"❌ Lost {amount:<5} on {choice:<4} → Payout 0"
            else:
                line = f"⚪️ Draw {amount:<5} on {choice:<4} → Refunded"
            recent_lines.append(line)

        embed.add_field(
            name="🗓️ Recent Bets",
            value="```" + "\n".join(recent_lines) + "```",
            inline=False
        )

    if dm:
        try:
            await target_user.send(embed=embed)
            await interaction.followup.send("✅ Stats sent via DM!", ephemeral=True)
        except:
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
            f"Differential: **{h.get('handicap_differential', 'N/A')}**"
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
        name = fixed_width_name(name)  # ← ✅ fixed width here
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

@tree.command(
    name="add_course",
    description="Admin: Add a new course with image and ratings"
)
@discord.app_commands.checks.has_permissions(administrator=True)
async def add_course(interaction: discord.Interaction):
    await interaction.response.send_modal(AddCourseModal())


@tree.command(
    name="set_course_rating",
    description="Admin: Update course par and avg. par via paginated dropdown"
)
@discord.app_commands.checks.has_permissions(administrator=True)
async def set_course_rating(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    res = await run_db(lambda: supabase.table("courses").select("*").execute())
    if not res.data:
        await interaction.followup.send("❌ No courses found.", ephemeral=True)
        return

    # ✅ Provide a custom callback for this use-case:
    async def on_select(inter: discord.Interaction, course_id):
        selected = next((c for c in res.data if str(c["id"]) == course_id), None)
        if not selected:
            await inter.response.send_message("❌ Course not found.", ephemeral=True)
            return

        await inter.response.send_modal(SetCourseRatingModal(selected))

    # ✅ Monkey-patch your view with this callback:
    class SetRatingPaginatedCourseSelect(PaginatedCourseSelect):
        async def callback(self, interaction: discord.Interaction):
            course_id = self.values[0]
            await on_select(interaction, course_id)

    class SetRatingPaginatedCourseView(PaginatedCourseView):
        def update_children(self):
            self.clear_items()
            start = self.page * self.per_page
            end = start + self.per_page
            page_courses = self.courses[start:end]

            options = [
                discord.SelectOption(label=c["name"], value=str(c["id"]))
                for c in page_courses
            ]
            self.add_item(SetRatingPaginatedCourseSelect(options, self))

            if self.page > 0:
                self.add_item(self.PrevButton(self))
            if end < len(self.courses):
                self.add_item(self.NextButton(self))

    view = SetRatingPaginatedCourseView(res.data)
    msg = await interaction.followup.send(
        "🎯 Pick a course to update:",
        view=view,
        ephemeral=True
    )
    view.message = await msg

async def update_course_average_par(course_id: str):
    """
    Recalculate and update the avg_par for the given course_id.
    """
    # 1) Get all scores for this course
    res = await run_db(lambda: supabase
        .table("handicaps")
        .select("score")
        .eq("course_id", course_id)
        .execute()
    )
    scores = [row["score"] for row in res.data or []]

    if not scores:
        # If no scores exist, skip update.
        return None

    new_avg = round(sum(scores) / len(scores), 1)

    # 2) Update the course row
    await run_db(lambda: supabase
        .table("courses")
        .update({"avg_par": new_avg})
        .eq("id", course_id)
        .execute()
    )

    return new_avg


class AdminSubmitScoreModal(discord.ui.Modal, title="Admin: Set Best Score"):
    def __init__(self, course_name: str, course_id: str, target_user: discord.User):
        super().__init__()

        self.course_name = course_name
        self.course_id = course_id
        self.target_user = target_user  # ✅ Carry the correct user!

        short_name = (course_name[:30] + "...") if len(course_name) > 30 else course_name

        self.best_score = discord.ui.TextInput(
            label=f"Best score for {short_name}",
            placeholder="e.g. 44",
            required=True
        )
        self.add_item(self.best_score)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            score = int(self.best_score.value.strip())
        except ValueError:
            await interaction.response.send_message("❌ Invalid score.", ephemeral=True)
            return

        # 1️⃣ Insert raw score for the target_user
        await run_db(lambda: supabase
            .table("handicaps")
            .upsert({
                "player_id": str(self.target_user.id),
                "course_id": self.course_id,
                "score": score
            })
            .execute()
        )

        # 2️⃣ Recompute average
        new_avg = await update_course_average_par(self.course_id)

        # 3️⃣ Compute & update the handicap for the same user
        handicap = score - new_avg

        await run_db(lambda: supabase
            .table("handicaps")
            .update({"handicap": handicap})
            .eq("player_id", str(self.target_user.id))
            .eq("course_id", self.course_id)
            .execute()
        )

        await interaction.response.send_message(
            f"✅ Updated **{self.target_user.display_name}**:\n"
            f"• Score: **{score}**\n"
            f"• Handicap: **{handicap:+.1f}**\n"
            f"• New avg par: **{new_avg:.1f}**",
            ephemeral=True
        )


@tree.command(
    name="get_user_id",
    description="Show the Discord ID of a chosen member"
)
@app_commands.describe(
    user="The user whose ID you want to get"
)
async def get_user_id(interaction: discord.Interaction, user: discord.User):
    await interaction.response.send_message(
        f"🆔 **{user.display_name}**'s Discord ID: `{user.id}`",
        ephemeral=True  # Only the caller can see it
    )


@bot.event
async def on_ready():
    # Sync the slash commands with Discord
    await tree.sync()
    print(f"✅ Logged in as {bot.user}")

    # Fetch and load any pending games
    pending = await load_pending_games()
    
    # Iterate over each pending game and start a new button if the channel exists
    for pg in pending:
        channel = bot.get_channel(pg["channel_id"])  # Fetch the channel where the game was pending
        if channel:
            # Call start_new_game_button with game_type and max_players from pending game
            # Ensure max_players is fetched correctly for each pending game
            max_players = pg.get("max_players", 2)  # Default to 2 players if max_players is not found
            await start_new_game_button(channel, pg["game_type"], max_players=max_players)


bot.run(os.getenv("DISCORD_BOT_TOKEN"))
