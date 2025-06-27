from typing import Optional
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
import uuid
from collections import defaultdict
from collections import Counter
from datetime import datetime, timedelta
import zoneinfo


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
intents = discord.Intents.all()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
bot.tournaments = {}
tree = bot.tree 


IS_TEST_MODE = os.getenv("TEST_MODE", "1") == "1"

TEST_PLAYER_IDS = [
    970268488239317023,
    807840646764429342,
    701689044635091124,
    1117780404011815003,
    769210966150742056,
    928692043780325448,
    1041382761996492830
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
    "credits": 1000,
    "stats": {
        "singles": {
            "rank": 1000,
            "wins": 0,
            "losses": 0,
            "draws": 0,
            "games_played": 0,
            "current_streak": 0,
            "best_streak": 0,
            "trophies": 0
        },
        "doubles": {
            "rank": 1000,
            "wins": 0,
            "losses": 0,
            "draws": 0,
            "games_played": 0,
            "current_streak": 0,
            "best_streak": 0,
            "trophies": 0
        },
        "triples": {
            "rank": 1000,
            "wins": 0,
            "losses": 0,
            "draws": 0,
            "games_played": 0,
            "current_streak": 0,
            "best_streak": 0,
            "trophies": 0
        },
        "tournaments": {
            "rank": 1000,
            "wins": 0,
            "losses": 0,
            "draws": 0,
            "games_played": 0,
            "current_streak": 0,
            "best_streak": 0,
            "trophies": 0
        }
    }
}


# Helpers
async def hourly_room_announcer(bot, lobby_channel_id):
    await bot.wait_until_ready()
    lobby_channel = bot.get_channel(lobby_channel_id)

    if not lobby_channel:
        print(f"[ERROR] Lobby channel ID {lobby_channel_id} not found!")
        return

    local_tz = zoneinfo.ZoneInfo("Europe/Copenhagen")  # ✅ your local time zone
    last_triggered_minute = None
    active_announcement = None

    while not bot.is_closed():
        now = datetime.now(tz=local_tz)
        minute = now.minute

        # Create game at HH:15 or HH:45
        if minute in (15, 45) and last_triggered_minute != minute:
            last_triggered_minute = minute
            timestamp = now.strftime("%H:%M")

            try:
                # Fetch random course
                res = await run_db(lambda: supabase.table("courses").select("id", "name", "image_url").execute())
                chosen = random.choice(res.data or [{}])
                course_name = chosen.get("name", "Unknown Course")
                course_image = chosen.get("image_url", "")

                # Generate unique room name
                room_name = await room_name_generator.get_unique_word()
                expire_ts = int((now + datetime.timedelta(minutes=15)).timestamp())

                embed = discord.Embed(
                    title=f"🕹️ Special Match Room: **{room_name.upper()}**",
                    description=(
                        f"**Course:** `{course_name}`\n"
                        f"**Start Time:** `{timestamp}`\n"
                        f"⏳ *Expires <t:{expire_ts}:R>*\n"
                        f"\n👍 React if you're interested!"
                    ),
                    color=discord.Color.gold()
                )

                if course_image:
                    embed.set_image(url=course_image)

                active_announcement = await lobby_channel.send(embed=embed)
                await active_announcement.add_reaction("👍")
                print(f"[INFO] Hourly room posted at {timestamp} with course: {course_name}")

            except Exception as e:
                print(f"[ERROR] Failed to post hourly room: {e}")

        # Expire game at HH:00 or HH:30
        elif minute in (0, 30) and last_triggered_minute != minute:
            last_triggered_minute = minute

            if active_announcement:
                try:
                    await active_announcement.edit(content="⚠️ This room has now expired.", embed=None, view=None)
                    print(f"[INFO] Hourly room expired at {now.strftime('%H:%M')}")
                except Exception as e:
                    print(f"[ERROR] Failed to expire room message: {e}")
                active_announcement = None

        await asyncio.sleep(10)  # Check every 10 seconds

def is_admin(interaction: discord.Interaction) -> bool:
    return interaction.user.guild_permissions.administrator

async def set_parameter(key: str, value: str):
    await run_db(
        lambda: supabase
            .table("parameters")
            .upsert({"key": key, "value": value})
            .execute()
    )

async def get_parameter(key: str):
    res = await run_db(
        lambda: supabase
            .table("parameters")
            .select("value")
            .eq("key", key)
            .execute()
    )
    if res and res.data:
        return res.data[0]["value"]
    return None


def resolve_bet_choice_name(choice, game_type, players=None, guild=None):
    choice = str(choice)
    
    if choice.upper() in ["A", "B"]:
        return f"Team {choice.upper()}"
    
    if game_type in ["singles", "triples", "tournament"]:
        try:
            idx = int(choice) - 1
            if players and 0 <= idx < len(players):
                pid = players[idx]
                if guild:
                    member = guild.get_member(pid)
                    return member.display_name if member else f"Player {choice}"
                return f"Player {choice}"
        except ValueError:
            pass

    try:
        # fallback: treat as Discord user ID
        member = guild.get_member(int(choice)) if guild else None
        return member.display_name if member else f"User {choice}"
    except:
        return f"User {choice}"

async def send_global_notification(game_type: str, lobby_link: str, guild: discord.Guild):
    """
    🔔 Send a push-worthy notification to the alerts channel, with @role ping, embed, and banner.
    """

    # 📌 Match each game type to its ping role
    ROLE_ID = 1387692640438456361

    # 📢 Channel to send alerts to
    ALERT_CHANNEL_ID = 1387693753631772844  # replace with your real game-alerts channel ID

    if not ROLE_ID:
        print(f"[WARN] Unknown game type: {game_type}")
        return

    role = guild.get_role(ROLE_ID)
    if not role:
        print(f"[WARN] Role ID {ROLE_ID} not found in guild {guild.name}")
        return

    channel = guild.get_channel(ALERT_CHANNEL_ID)
    if not channel:
        print(f"[ERROR] Channel ID {ALERT_CHANNEL_ID} not found in guild {guild.name}")
        return

    embed = discord.Embed(
        title="🏌️ **Mini Golf Misfits**",
        description=(
            f"A new **`{game_type}`** lobby just opened!\n\n"
            f"[👉 **Click here to join the lobby!**]({lobby_link})"
        ),
        color=discord.Color.green()
    )
    embed.set_image(
        url="https://nxybekwiefwxnijrwuas.supabase.co/storage/v1/object/public/game-images/banner.png"
    )
    embed.set_footer(text="League of Extraordinary Misfits")

    await channel.send(
        content=f"{role.mention} ⛳ **New `{game_type}` game alert!**",
        embed=embed,
        allowed_mentions=discord.AllowedMentions(roles=True)
    )

    print(f"[INFO] Global alert sent for '{game_type}' to #{channel.name}")



async def expected_score(rating_a, rating_b):
    """Expected score for player/team A vs B"""
    return 1 / (1 + 10 ** ((rating_b - rating_a) / 400))

async def update_elo_pair_and_save(player1_id, player2_id, winner, k=32, game_type="singles"):
    """
    Singles: ELO + stats per game_type.
    winner: 1 (player1), 2 (player2), 0.5 (draw)
    """
    p1 = await get_player(player1_id)
    p2 = await get_player(player2_id)

    # ✅ Safely initialize full stat block
    default_stats = {
        "rank": 1000,
        "wins": 0,
        "losses": 0,
        "draws": 0,
        "games_played": 0,
        "current_streak": 0,
        "best_streak": 0,
        "trophies": 0
    }

    p1.setdefault("stats", {}).setdefault(game_type, default_stats.copy())
    p2.setdefault("stats", {}).setdefault(game_type, default_stats.copy())

    s1 = p1["stats"][game_type]
    s2 = p2["stats"][game_type]

    r1 = s1["rank"]
    r2 = s2["rank"]

    e1 = await expected_score(r1, r2)

    if winner == 1:
        actual1 = 1
    elif winner == 2:
        actual1 = 0
    else:
        actual1 = 0.5

    delta = round(k * (actual1 - e1))

    s1["rank"] += delta
    s2["rank"] -= delta

    s1["games_played"] += 1
    s2["games_played"] += 1

    if winner == 1:
        s1["wins"] += 1
        s2["losses"] += 1
        s1["current_streak"] += 1
        s2["current_streak"] = 0
        s1["best_streak"] = max(s1["best_streak"], s1["current_streak"])
        s1["trophies"] += 1
    elif winner == 2:
        s2["wins"] += 1
        s1["losses"] += 1
        s2["current_streak"] += 1
        s1["current_streak"] = 0
        s2["best_streak"] = max(s2["best_streak"], s2["current_streak"])
        s2["trophies"] += 1
    else:
        s1["draws"] += 1
        s2["draws"] += 1
        s1["current_streak"] = 0
        s2["current_streak"] = 0

    await save_player(player1_id, p1)
    await save_player(player2_id, p2)

    print(f"[ELO] {game_type.title()}: {player1_id} {r1} → {s1['rank']} | {player2_id} {r2} → {s2['rank']}")
    return s1["rank"], s2["rank"]



async def update_elo_doubles_and_save(teamA_ids, teamB_ids, winner, k=32, game_type="doubles"):
    teamA = [await get_player(pid) for pid in teamA_ids]
    teamB = [await get_player(pid) for pid in teamB_ids]

    for p in teamA + teamB:
        p.setdefault("stats", {})
        p["stats"].setdefault(game_type, {
            "rank": 1000,
            "games_played": 0,
            "wins": 0,
            "losses": 0,
            "draws": 0,
            "trophies": 0,
            "current_streak": 0,
            "best_streak": 0,
        })

    avgA = sum(p["stats"][game_type]["rank"] for p in teamA) / 2
    avgB = sum(p["stats"][game_type]["rank"] for p in teamB) / 2

    eA = await expected_score(avgA, avgB)

    if winner.upper() == "A":
        sA, sB = 1, 0
    elif winner.upper() == "B":
        sA, sB = 0, 1
    else:
        sA, sB = 0.5, 0.5

    delta = round(k * (sA - eA))

    for idx, p in enumerate(teamA):
        s = p["stats"][game_type]
        old = s["rank"]
        s["rank"] += delta
        s["games_played"] += 1

        if sA > sB:
            s["wins"] += 1
            s["trophies"] += 1
            s["current_streak"] += 1
            s["best_streak"] = max(s["best_streak"], s["current_streak"])
        elif sA < sB:
            s["losses"] += 1
            s["current_streak"] = 0
        else:
            s["draws"] += 1
            s["current_streak"] = 0

        await save_player(teamA_ids[idx], p)
        print(f"[ELO] Team A Player {teamA_ids[idx]}: {old} → {s['rank']}")

    for idx, p in enumerate(teamB):
        s = p["stats"][game_type]
        old = s["rank"]
        s["rank"] -= delta
        s["games_played"] += 1

        if sB > sA:
            s["wins"] += 1
            s["trophies"] += 1
            s["current_streak"] += 1
            s["best_streak"] = max(s["best_streak"], s["current_streak"])
        elif sB < sA:
            s["losses"] += 1
            s["current_streak"] = 0
        else:
            s["draws"] += 1
            s["current_streak"] = 0

        await save_player(teamB_ids[idx], p)
        print(f"[ELO] Team B Player {teamB_ids[idx]}: {old} → {s['rank']}")

    return [p["stats"][game_type]["rank"] for p in teamA], [p["stats"][game_type]["rank"] for p in teamB]


async def update_elo_triples_and_save(player_ids, winner, k=32, game_type="triples"):
    """
    Triples: free-for-all ELO + per-game-type stats.
    winner: player_id
    """
    default_stats = {
        "rank": 1000,
        "wins": 0,
        "losses": 0,
        "draws": 0,
        "games_played": 0,
        "current_streak": 0,
        "best_streak": 0,
        "trophies": 0
    }

    players = [await get_player(pid) for pid in player_ids]
    stats_list = []

    for p in players:
        p.setdefault("stats", {})
        p["stats"].setdefault(game_type, default_stats.copy())
        stats_list.append(p["stats"][game_type])

    # Compute current ranks
    ranks = [s["rank"] for s in stats_list]

    # Expected score for each player
    exp = [10 ** (r / 400) for r in ranks]
    total = sum(exp)
    expected = [v / total for v in exp]

    for idx, p in enumerate(players):
        s = stats_list[idx]
        pid = player_ids[idx]

        S = 1 if pid == winner else 0
        E = expected[idx]

        old_rank = s["rank"]
        s["rank"] = round(old_rank + k * (S - E))

        s["games_played"] += 1

        if S == 1:
            s["wins"] += 1
            s["trophies"] += 1
            s["current_streak"] += 1
            s["best_streak"] = max(s["best_streak"], s["current_streak"])
        else:
            s["losses"] += 1
            s["current_streak"] = 0

        await save_player(pid, players[idx])
        print(f"[ELO] Triples Player {pid}: {old_rank} → {s['rank']}")

    return [s["rank"] for s in stats_list]


async def update_elo_series_and_save(player1_id, player2_id, results, k=32, game_type="singles"):
    """
    Multiple rounds ELO + stats, scoped by game_type.
    - results: list of outcomes per round: 1, 2, or 0.5 (draw)
    Returns final ELOs for both players for this mode.
    """
    default_stats = {
        "rank": 1000,
        "wins": 0,
        "losses": 0,
        "draws": 0,
        "games_played": 0,
        "current_streak": 0,
        "best_streak": 0,
        "trophies": 0
    }

    p1 = await get_player(player1_id)
    p2 = await get_player(player2_id)

    p1.setdefault("stats", {}).setdefault(game_type, default_stats.copy())
    p2.setdefault("stats", {}).setdefault(game_type, default_stats.copy())

    s1 = p1["stats"][game_type]
    s2 = p2["stats"][game_type]

    r1 = s1["rank"]
    r2 = s2["rank"]

    for outcome in results:
        e1 = await expected_score(r1, r2)
        if outcome == 1:
            s_actual, o_actual = 1, 0
        elif outcome == 2:
            s_actual, o_actual = 0, 1
        else:
            s_actual, o_actual = 0.5, 0.5

        delta = round(k * (s_actual - e1))
        r1 += delta
        r2 -= delta

    # Final updated ranks after series
    s1["rank"] = r1
    s2["rank"] = r2

    # Series counted as one game
    s1["games_played"] += 1
    s2["games_played"] += 1

    total = sum(results)
    rounds = len(results)

    if total > rounds / 2:
        # p1 wins
        s1["wins"] += 1
        s2["losses"] += 1
        s1["current_streak"] += 1
        s2["current_streak"] = 0
        s1["best_streak"] = max(s1["best_streak"], s1["current_streak"])
        s1["trophies"] += 1
    elif total < rounds / 2:
        # p2 wins
        s2["wins"] += 1
        s1["losses"] += 1
        s2["current_streak"] += 1
        s1["current_streak"] = 0
        s2["best_streak"] = max(s2["best_streak"], s2["current_streak"])
        s2["trophies"] += 1
    else:
        # draw
        s1["draws"] += 1
        s2["draws"] += 1
        s1["current_streak"] = 0
        s2["current_streak"] = 0

    await save_player(player1_id, p1)
    await save_player(player2_id, p2)

    print(f"[ELO] {game_type.title()} Series updated {player1_id}: {r1} | {player2_id}: {r2}")
    return r1, r2


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

    target_id = int(choice)  # or however you're storing it
    member = interaction.guild.get_member(target_id)
    target_name = member.display_name if member else f"User {target_id}"

    await interaction.response.send_message(
        f"✅ Bet of {amount} placed on {target_name}. Potential payout: {payout}",
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


async def update_user_stat(user_id, key, value, mode="set", game_type=None):
    res = await run_db(lambda: supabase.table("players").select("*").eq("id", str(user_id)).single().execute())

    if res.data is None:
        data = default_template.copy()
        data["id"] = str(user_id)
    else:
        data = res.data

    if game_type:
        stats_branch = data.setdefault("stats", {}).setdefault(game_type, {})
        if mode == "set":
            stats_branch[key] = value
        elif mode == "add":
            stats_branch[key] = stats_branch.get(key, 0) + value
    else:
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

    # ✅ 1) Clean up old button message
    old = start_buttons.get(key)
    if old:
        try:
            await old.delete()
            print(f"🗑️ Deleted old start button for {game_type} in #{channel.name}")
        except discord.NotFound:
            print(f"⚠️ Old button already deleted for {game_type} in #{channel.name}")
        except Exception as e:
            print(f"⚠️ Could not delete old start button: {e}")


    # ✅ 3) Create a FRESH Join View
    if game_type == "tournament":
        view = TournamentStartButtonView()
        msg = await channel.send("🏆 Click to start a **Tournament**:", view=view)
    else:
        view = GameJoinView(game_type, max_players)
        msg = await channel.send(f"🎮 Start a new {game_type} game:", view=view)

    # ✅ 4) Store only the message — not the view itself
    start_buttons[key] = msg

    print(f"✅ New start button posted for {game_type} in #{channel.name}")

    return msg




async def show_betting_phase(self):
    self.clear_items()
    self.add_item(BettingButtonDropdown(self))
    if self.betting_task:
        self.betting_task.cancel()
    if self.message:
        await self.message.edit(embed=await self.build_embed(self.message.guild), view=self)

    self.betting_task = asyncio.create_task(self._betting_countdown())
    self.betting_closed = True
    self.clear_items()
    await self.update_message()


async def update_message(self, no_image=True, status=None):
    if not self.message:
        print("[update_message] SKIPPED: no message to update.")
        return

    # ✅ SAFETY: do not edit if ended!
    if self.game_has_ended:
        print("[update_message] SKIPPED: game already ended.")
        return

    embed = await self.build_embed(self.message.guild, no_image=no_image, status=status)
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

        # ✅ Use dynamic label
        button = discord.ui.Button(
            label=f"Start {self.game_type} game",
            style=discord.ButtonStyle.primary
        )
        button.callback = self.start_game
        self.add_item(button)
    
    async def start_game(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # ✅ Block duplicate games of same type
        if pending_games.get(self.game_type):
            await interaction.followup.send(
                "⚠️ A game of this type is already pending.",
                ephemeral=True
            )
            return

        # ✅ Block ANY other active game (cross-lobby)
        if player_manager.is_active(interaction.user.id):
            await interaction.followup.send(
                "🚫 You are already in another game or must finish voting first.",
                ephemeral=True
            )
            return

        # ✅ Delete old start button
        try:
            await interaction.message.delete()
        except:
            pass

        # ✅ Make fresh GameView
        view = GameView(
            self.game_type,
            interaction.user.id,
            self.max_players,
            interaction.channel
        )

        player_manager.activate(interaction.user.id)

        # ✅ TEST MODE: auto-fill dummy players
        if IS_TEST_MODE:
            for pid in TEST_PLAYER_IDS:
                if pid != interaction.user.id and pid not in view.players and len(view.players) < view.max_players:
                    view.players.append(pid)
                    player_manager.activate(pid)

        # ✅ Post the lobby
        embed = await view.build_embed(interaction.guild, no_image=True)
        view.message = await interaction.channel.send(embed=embed, view=view)
        pending_games[self.game_type] = view

        # ✅ If full immediately → auto start
        if len(view.players) == view.max_players:
            await view.game_full(interaction)
        else:
            await send_global_notification(
                self.game_type,
                view.message.jump_url,
                interaction.guild
            )

class HandicapLeaderboardView(discord.ui.View):
    def __init__(self, player_name, all_data, requester_name, per_page=10):
        super().__init__(timeout=60)  # auto-timeout
        self.player_name = player_name
        self.requester_name = requester_name
        self.data = all_data
        self.per_page = per_page
        self.page = 0

    def get_page_data(self):
        start = self.page * self.per_page
        end = start + self.per_page
        page_items = self.data[start:end]

        lines = []
        lines.append(f"`{'#':<3} {'Course':<20} {'Handicap':>8}`")
        lines.append(f"`{'—'*35}`")

        for i, row in enumerate(page_items, start=1 + start):
            course_name = row['courses']['name'][:20]
            handicap = f"{row['handicap']:.1f}"
            lines.append(f"`{i:<3} {course_name:<20} {handicap:>8}`")

        leaderboard = "\n".join(lines)
        return leaderboard

    def create_embed(self):
        embed = discord.Embed(
            title=f"🏆 {self.player_name}'s Course Handicaps (Page {self.page + 1}/{self.total_pages()})",
            description=self.get_page_data(),
            color=discord.Color.gold()
        )
        embed.set_footer(text=f"Requested by {self.requester_name}")
        return embed

    def total_pages(self):
        return (len(self.data) + self.per_page - 1) // self.per_page

    @discord.ui.button(label="⬅️ Previous", style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
            await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label="➡️ Next", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page < self.total_pages() - 1:
            self.page += 1
            await interaction.response.edit_message(embed=self.create_embed(), view=self)

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


class BettingDropdownView(discord.ui.View):
    def __init__(self, game_view):
        super().__init__(timeout=None)
        self.dropdown = BetDropdown(game_view)
        self.add_item(self.dropdown)

    async def prepare(self):
        await self.dropdown.build_options()


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

            # ✅ Compute odds
            odds_provider = getattr(self.game_view, "_embed_helper", self.game_view)
            odds = await odds_provider.get_odds(choice)
            payout = max(1, int(amount / odds)) if odds > 0 else amount

            # ✅ Deduct credits
            success = await deduct_credits_atomic(user_id, amount)
            if not success:
                await interaction.response.send_message("❌ Not enough credits to place this bet.", ephemeral=True)
                return

            # ✅ Insert into database
            await run_db(lambda: supabase
                .table("bets")
                .insert({
                    "player_id": str(user_id),
                    "game_id": self.game_view.message.id,
                    "choice": choice,
                    "amount": amount,
                    "payout": payout,
                    "won": None
                }).execute()
            )

            # ✅ Register live bet in memory
            await self.game_view.add_bet(user_id, interaction.user.display_name, amount, choice, interaction)

            # ✅ Attempt to resolve choice to a display name
            guild = self.game_view.message.guild if self.game_view.message else None
            players = getattr(self.game_view, "players", [])
            target_name = str(choice)

            if choice.upper() in ["A", "B"]:
                target_name = f"Team {choice.upper()}"
            else:
                try:
                    idx = int(choice) - 1
                    if 0 <= idx < len(players):
                        pid = players[idx]
                        member = guild.get_member(pid) if guild else None
                        target_name = member.display_name if member else f"Player {choice}"
                    else:
                        member = guild.get_member(int(choice)) if guild else None
                        target_name = member.display_name if member else f"User {choice}"
                except:
                    pass  # fallback to raw choice

            # ✅ Response
            await interaction.response.send_message(
                f"✅ Bet placed!\n• Choice: **{target_name}**\n• Bet: **{amount}**\n• Odds: **{odds * 100:.1f}%**\n• Payout: **{payout}**",
                ephemeral=True
            )

        except Exception as e:
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
        elif game_type == "tournament":
            for i, player_id in enumerate(players, start=1):
                member = guild.get_member(player_id) if guild else None
                name = member.display_name if member else f"Player {i}"
                name = fixed_width_name(name)
                options.append(discord.SelectOption(
                    label=name,
                    value=str(player_id)  # ✅ use raw ID as the value!
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
    def __init__(self, bot, guild, players, game_type, room_name, lobby_message=None, lobby_embed=None, game_view=None, course_name=None, course_id=None, max_players=2):
        super().__init__(timeout=None)
        self.bot = bot             # ✅ store bot
        self.guild = guild     
        self.players = [p.id if hasattr(p, "id") else p for p in players]
        self.game_type = game_type
        self.room_name = room_name
        self.message = None  # thread message
        self.lobby_message = lobby_message
        self.channel = self.message.channel if self.message else None
        self.lobby_embed = lobby_embed
        self.game_view = game_view
        self.max_players = max_players  # ✅ store it!
        self.betting_task = None
        self.betting_closed = False

        # ✅ Store course_name robustly:
        self.course_name = course_name or getattr(game_view, "course_name", None)
        self.course_id = course_id or getattr(game_view, "course_id", None)

        self.votes = {}
        self.vote_timeout = None
        self.game_has_ended = False
        self.voting_closed = False
        self.add_item(GameEndedButton(self))
        self.on_tournament_complete = None


    def cancel_abandon_task(self):
        if hasattr(self, "abandon_task") and self.abandon_task:
            self.abandon_task.cancel()
            self.abandon_task = None

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
        player_lines = []

        # Optional: prepare ranks, odds, handicaps as needed:
        ranks = []
        for p in self.players:
            pdata = await get_player(p)
            ranks.append(pdata.get("rank", 1000))

        # Compute odds if needed:
        odds = []
        if self.game_type == "doubles" and len(self.players) >= 4:
            e1 = sum(ranks[:2]) / 2
            e2 = sum(ranks[2:]) / 2
            odds_a = 1 / (1 + 10 ** ((e2 - e1) / 400))
            odds_b = 1 - odds_a
        elif self.game_type == "triples" and len(self.players) >= 3:
            exp = [10 ** (r / 400) for r in ranks]
            total = sum(exp)
            odds = [v / total for v in exp]

        game_full = len(self.players) == self.max_players

        # Team A label for doubles:
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

                # For RoomView you may skip HCP unless you have course_id handy:
                hcp_txt = ""

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


        # ✅ 3️⃣ Add Players field BELOW description
        embed.add_field(name="👥 Players", value="\n".join(player_lines), inline=False)

        # ✅ 4️⃣ Add status field
        embed.add_field(name="🎮 Status", value="Match in progress.", inline=True)

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
        embed.add_field(name="🎮 Status", value="Game has ended.", inline=True)

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
                if label.lower().startswith("vote "):
                    label = label
                else:
                    label = f"Vote {label}"
            self.add_item(VoteButton(option, self, label))

        # ✅ 1️⃣ REBUILD embed for voting
        embed = await self.build_lobby_end_embed(winner=None)

        # ✅ 2️⃣ Edit with fresh embed + fresh voting view
        await self.message.edit(embed=embed, view=self)

        # ✅ 3️⃣ Start timeout
        self.vote_timeout = asyncio.create_task(self.end_voting_after_timeout())

    def cancel_vote_timeout(self):
        if hasattr(self, "vote_timeout") and self.vote_timeout:
            self.vote_timeout.cancel()
            self.vote_timeout = None

    async def end_voting_after_timeout(self):
        await asyncio.sleep(600)
        await self.finalize_game()

    async def finalize_game(self):

        # ✅ Cancel timers
        self.cancel_abandon_task()
        self.cancel_vote_timeout()

        if self.game_view:
            self.game_view.game_has_ended = True
            self.game_view.cancel_betting_task()

        self.game_has_ended = True

        # ✅ Count votes
        self.votes = {uid: val for uid, val in self.votes.items() if uid in self.players}
        vote_counts = Counter(self.votes.values())
        most_common = vote_counts.most_common()

        if not most_common:
            winner = None
        elif len(most_common) > 1 and most_common[0][1] == most_common[1][1]:
            winner = "draw"
        else:
            winner = most_common[0][0]

        self.voting_closed = True

        # ✅ DRAW CASE — refund bets, update stats
        if winner == "draw":
            for p in self.players:
                pdata = await get_player(p)
                pdata["draws"] += 1
                pdata["games_played"] += 1
                pdata["current_streak"] = 0
                await save_player(p, pdata)

            if self.game_view:
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

            embed = await self.build_lobby_end_embed(winner)
            await self.message.edit(embed=embed, view=None)

            if self.lobby_message and self.game_view:
                lobby_embed = await self.game_view.build_embed(self.lobby_message.guild, winner=winner, no_image=True)
                await self.lobby_message.edit(embed=lobby_embed, view=None)

            await self.channel.send("🤝 Voting ended in a **draw** — all bets refunded.")
            await self.channel.edit(archived=True)
            pending_games[self.game_type] = None
            return

        # ✅ WIN CASE — normalize
        normalized_winner = normalize_team(winner) if self.game_type == "doubles" else winner

        if self.game_type == "singles":
            await update_elo_pair_and_save(
                self.players[0],
                self.players[1],
                winner=1 if self.players[0] == winner else 2
            )
        elif self.game_type == "doubles":
            await update_elo_doubles_and_save(
                self.players[:2],
                self.players[2:],
                winner=normalized_winner
            )
        elif self.game_type == "triples":
            await update_elo_triples_and_save(
                self.players,
                winner
            )

        # ✅ Process bets
        if self.game_view:
            for uid, uname, amount, choice in self.game_view.bets:
                won = False

                if self.game_type == "singles":
                    won = (choice == "1" and self.players[0] == winner) or \
                          (choice == "2" and self.players[1] == winner)
                elif self.game_type == "doubles":
                    won = normalize_team(choice) == normalized_winner
                elif self.game_type == "triples":
                    try:
                        idx = int(choice) - 1
                        won = self.players[idx] == winner
                    except:
                        won = False

                # ✅ Store win/loss first
                await run_db(lambda: supabase
                    .table("bets")
                    .update({"won": won})
                    .eq("player_id", uid)
                    .eq("game_id", self.game_view.message.id)
                    .eq("choice", choice)
                    .execute()
                )

                # ✅ THEN calculate payout for THIS bet
                if won:
                    odds = await self.game_view.get_odds(choice)
                    payout = int(amount * (1 / odds)) if odds > 0 else amount
                    await add_credits_internal(uid, payout)
                    print(f"💰 {uname} won! Payout: {payout}")
                else:
                    payout = 0
                    print(f"❌ {uname} lost {amount}")

                # ✅ Store payout for THIS bet
                await run_db(lambda: supabase
                    .table("bets")
                    .update({"payout": payout})
                    .eq("player_id", uid)
                    .eq("game_id", self.game_view.message.id)
                    .eq("choice", choice)
                    .execute()
                )

        # ✅ 4️⃣ Final embeds
        winner_name = winner
        if isinstance(winner, int):
            member = self.message.guild.get_member(winner)
            winner_name = member.display_name if member else f"User {winner}"

        embed = await self.build_lobby_end_embed(winner)
        await self.message.edit(embed=embed, view=None)

        target_message = self.lobby_message or (self.game_view.message if self.game_view else None)
        if target_message and self.game_view:
            lobby_embed = await self.game_view.build_embed(target_message.guild, winner=winner, no_image=True)
            for item in list(self.game_view.children):
                if isinstance(item, BettingButton) or getattr(item, "label", "") == "Place Bet":
                    self.game_view.remove_item(item)
            await target_message.edit(embed=lobby_embed, view=self.game_view)

        self.players = []
        await self.channel.send(f"🏁 Voting ended. Winner: **{winner_name}**")
        await asyncio.sleep(3)
        await self.channel.edit(archived=True)
        pending_games[self.game_type] = None

        # ✅ Robust: delete active_game row with fallback ID
        target_game_id = (
            str(self.lobby_message.id)
            if self.lobby_message else
            str(self.game_view.message.id) if self.game_view and self.game_view.message else
            str(self.message.id) if self.message else None
        )
        if target_game_id:
            await run_db(lambda: supabase
                .table("active_games")
                .delete()
                .eq("game_id", target_game_id)
                .execute()
            )
            print(f"[finalize_game] ✅ Deleted active_game for {target_game_id}")
        else:
            print("[finalize_game] ⚠️ No valid game_id found to delete active_game row.")

        if self.on_tournament_complete:
            if isinstance(winner, int):
                await self.on_tournament_complete(winner)
            else:
                # ✅ Fallback for draw or no votes — pick a random winner so the tournament can continue
                print(f"[Tournament] No clear winner — randomly picking from: {self.players}")
                fallback = random.choice(self.players)
                await self.on_tournament_complete(fallback)

        await update_leaderboard(self.bot, self.game_type)



class GameEndedButton(discord.ui.Button):
    def __init__(self, view):
        super().__init__(label="End Game", style=discord.ButtonStyle.danger)
        self.view_obj = view  # RoomView

    async def callback(self, interaction: discord.Interaction):
        self.view_obj.game_has_ended = True
        if self.view_obj.game_view:
            self.view_obj.game_view.game_has_ended = True

        self.view_obj.betting_closed = True

        # ✅ 1️⃣ THREAD embed
        thread_embed = self.view_obj.lobby_embed.copy()
        thread_embed.set_footer(text="🎮 Game has ended.")
        await self.view_obj.message.edit(embed=thread_embed, view=None)

        # ✅ 2️⃣ Start voting
        await self.view_obj.start_voting()
        await interaction.response.defer()

        # ✅ 3️⃣ MAIN LOBBY embed
        target_message = self.view_obj.lobby_message
        if not target_message and self.view_obj.game_view:
            target_message = self.view_obj.game_view.message
        if target_message:
            updated_embed = await self.view_obj.game_view.build_embed(
                target_message.guild,
                winner=None,   # ✅ Proper: not "ended"
                no_image=True,
                status="🎮 Game ended."  # ✅ Force correct text
            )

            # ✅ Remove betting buttons
            for item in list(self.view_obj.children):
                if isinstance(item, BettingButtonDropdown) or isinstance(item, BettingButton):
                    self.view_obj.remove_item(item)
            for item in list(self.view_obj.game_view.children):
                if isinstance(item, BettingButtonDropdown) or isinstance(item, BettingButton):
                    self.view_obj.game_view.remove_item(item)

            await target_message.edit(embed=updated_embed, view=self.view_obj.game_view)


class VoteButton(discord.ui.Button):
    def __init__(self, value, view, raw_label):
        if raw_label.lower().startswith("vote "):
            label = raw_label
        else:
            label = f"Vote {raw_label}"
        super().__init__(label=label, style=discord.ButtonStyle.primary)
        self.value = value
        self.view_obj = view

    async def callback(self, interaction: discord.Interaction):
        if self.view_obj.voting_closed:
            await interaction.response.send_message("❌ Voting has ended.", ephemeral=True)
            return

        # ✅ NEW: Only allow actual match players to vote!
        if not IS_TEST_MODE and interaction.user.id not in self.view_obj.players:
            await interaction.response.send_message(
                "🚫 You are not a player in this match — you cannot vote.",
                ephemeral=True
            )
            return

        # ✅ Save the vote in the RoomView memory
        self.view_obj.votes[interaction.user.id] = self.value

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
            creator=interaction.user,
            view=self
        )
        await interaction.response.send_modal(modal)



class GameView(discord.ui.View):
    def __init__(self, game_type, creator, max_players, channel):
        super().__init__(timeout=None)
        self.game_type = game_type
        self.creator = creator
        self.players = [creator]
        self.max_players = max_players
        self.channel = channel
        self.message = None
        self.betting_closed = False
        self.bets = []
        self.betting_task = None
        self.course_image = None
        self.on_tournament_complete = None
        self.game_has_ended = False
        self.thread = None
        self.has_started = False  # ✅ add this

        # ✅ Unique ID per game for safe countdown
        self.instance_id = uuid.uuid4().hex

        self.add_item(LeaveGameButton(self))

    @discord.ui.button(label="Join Game", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id in self.players:
            await self.safe_send(interaction, "✅ You have already joined this game.", ephemeral=True)
            return

        if len(self.players) >= self.max_players:
            await self.safe_send(interaction, "🚫 This game is already full.", ephemeral=True)
            return

        if player_manager.is_active(interaction.user.id):
            await self.safe_send(interaction, "🚫 You are already in another active game or must finish voting first.", ephemeral=True)
            return

        player_manager.activate(interaction.user.id)
        self.players.append(interaction.user.id)
        await interaction.response.defer()
        await self.update_message()

        if len(self.players) == self.max_players:
            await self.game_full(interaction)

    def cancel_betting_task(self):
        if self.betting_task:
            self.betting_task.cancel()
            self.betting_task = None

    def cancel_abandon_task(self):
        if hasattr(self, "abandon_task") and self.abandon_task:
            self.abandon_task.cancel()
            self.abandon_task = None

    async def abandon_game(self, reason):
        self.cancel_abandon_task()
        self.cancel_betting_task()
        pending_games[self.game_type] = None

        for p in self.players:
            player_manager.deactivate(p)

        embed = discord.Embed(title="❌ Game Abandoned", description=reason, color=discord.Color.red())
        if self.message:
            try:
                await self.message.edit(embed=embed, view=None)
            except:
                pass

        self.message = None
        await start_new_game_button(self.channel, self.game_type, self.max_players)
        print(f"[abandon_game] New start posted for {self.game_type} in #{self.channel.name}")

    async def _betting_countdown(self, instance_id):
        print(f"[BET] Betting countdown started for instance {instance_id}")
        try:
            await asyncio.sleep(120)
            if self.instance_id != instance_id:
                print(f"[BET] Skipped: instance changed.")
                return
            if self.game_has_ended or self.betting_closed:
                print(f"[BET] Skipped: already ended or closed.")
                return

            self.betting_closed = True
            if hasattr(self, "betting_button"):
                self.remove_item(self.betting_button)
                self.betting_button = None

            await self.update_message(status="🕐 Betting closed. Good luck!")
            print(f"[BET] Betting closed for instance {instance_id}")
        except asyncio.CancelledError:
            print(f"[BET] Countdown cancelled for instance {instance_id}")

    async def show_betting_phase(self):
        self.clear_items()
        self.betting_button = BettingButtonDropdown(self)
        self.add_item(self.betting_button)
        await self.update_message(status="✅ Match is full. Place your bets!")

        if self.betting_task:
            self.betting_task.cancel()
        self.betting_task = asyncio.create_task(self._betting_countdown(self.instance_id))

    async def game_full(self, interaction):
        global pending_games
        self.cancel_abandon_task()
        self.cancel_betting_task()

        self.has_started = True 
        
        pending_games.pop(self.game_type, None)

        #await save_pending_game(self.game_type, self.players, self.channel.id, self.max_players)

        lobby_embed = await self.build_embed(interaction.guild, no_image=True)
        lobby_embed.title = f"{self.game_type.title()} Game Lobby"
        lobby_embed.color = discord.Color.orange()

        self.clear_items()
        self.betting_button = BettingButtonDropdown(self)
        self.add_item(self.betting_button)

        if not self.channel:
            self.channel = interaction.channel

        if self.message:
            try:
                await self.message.edit(embed=lobby_embed, view=self)
            except discord.NotFound:
                self.message = await self.channel.send(embed=lobby_embed, view=self)
        else:
            self.message = await self.channel.send(embed=lobby_embed, view=self)

        res = await run_db(lambda: supabase.table("courses").select("id", "name", "image_url").execute())
        chosen = random.choice(res.data or [{}])
        self.course_id = chosen.get("id")
        self.course_name = chosen.get("name", "Unknown")
        self.course_image = chosen.get("image_url", "")

        room_name = await room_name_generator.get_unique_word()
        thread = await interaction.channel.create_thread(
            name=room_name,
            type=discord.ChannelType.private_thread,
            invitable=False
        )
        self.thread = thread

        for pid in self.players:
            member = interaction.guild.get_member(pid)
            if member:
                await thread.add_user(member)

        thread_embed = await self.build_embed(interaction.guild, no_image=False)
        thread_embed.title = f"Game Room: {room_name}"
        thread_embed.description = f"Course: {self.course_name}"

        room_view = RoomView(
            bot=bot,
            guild=interaction.guild,
            players=self.players,
            game_type=self.game_type,
            room_name=room_name,
            lobby_message=self.message,
            lobby_embed=thread_embed,
            game_view=self,
            course_name=self.course_name,
            course_id=self.course_id,
            max_players=self.max_players
        )
        room_view.channel = thread
        room_view.original_embed = thread_embed.copy()

        mentions = " ".join(f"<@{p}>" for p in self.players)
        thread_msg = await thread.send(content=f"{mentions}\nMatch started!", embed=thread_embed, view=room_view)
        room_view.message = thread_msg

        await save_game_state(self, self, room_view)
        await start_new_game_button(self.channel, self.game_type, self.max_players)
        await self.show_betting_phase()

    async def update_message(self, status=None):
        if not self.message:
            print("[update_message] SKIPPED: no message to update.")
            return

        embed = await self.build_embed(self.message.guild, bets=self.bets, status=status)
        self.clear_items()

        # ✅ Only show Join/Leave if game hasn't started or ended
        if not self.betting_closed and not self.has_started:
            if len(self.players) < self.max_players:
                join_button = discord.ui.Button(label="Join Game", style=discord.ButtonStyle.success)

                async def join_callback(interaction: discord.Interaction):
                    await self.join(interaction, join_button)

                join_button.callback = join_callback
                self.add_item(join_button)

            self.add_item(LeaveGameButton(self))

        # ✅ Betting button (still allowed until betting is closed)
        if not self.betting_closed and hasattr(self, "betting_button"):
            self.add_item(self.betting_button)

        await self.message.edit(embed=embed, view=self)



    async def build_embed(self, guild=None, winner=None, no_image=True, status=None, bets=None):
        # Title
        title = "🏆 Tournament Lobby" if self.game_type == "tournament" else f"🎮 {self.game_type.title()} Match Lobby"
        
        if bets is None:
            bets = self.bets

        print(">>> BUILD EMBED DEBUG:", winner, self.game_has_ended, self.betting_closed)
        if status is not None and not self.game_has_ended:
            description = status
        elif self.game_has_ended or winner:
            description = "🎮 Game ended."
        elif self.betting_closed:
            description = "🕐 Betting closed. Good luck!"
        elif len(self.players) == self.max_players:
            description = "✅ Match is full. Place your bets!"
        else:
            description = "Awaiting players for a new match..."

        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.orange() if not winner else discord.Color.dark_gray(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_author(
            name="LEAGUE OF EXTRAORDINARY MISFITS",
            icon_url="https://cdn.discordapp.com/attachments/1378860910310854666/1382601173932183695/LOGO_2.webp"
        )

        if not no_image and getattr(self, "course_image", None):
            embed.set_image(url=self.course_image)

        # Gather player data
        ranks, handicaps = [], []
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
            e1, e2 = sum(ranks[:2]) / 2, sum(ranks[2:]) / 2
            odds_a = 1 / (1 + 10 ** ((e2 - e1) / 400))
            odds_b = 1 - odds_a
        elif self.game_type == "triples" and game_full:
            exp = [10 ** (r / 400) for r in ranks]
            total = sum(exp)
            odds = [v / total for v in exp]

        # Players section
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

        # Bets section — ✅ normalized & clear
        if bets:
            bet_lines = []
            for _, uname, amt, ch in bets:
                if self.game_type == "singles":
                    label = "Player 1" if ch == "1" else "Player 2"
                elif self.game_type == "doubles":
                    norm = normalize_team(ch)
                    label = f"Team {norm}" if norm in ("A", "B") else ch
                elif self.game_type == "triples":
                    label = f"Player {ch}"
                elif self.game_type == "tournament":
                    try:
                        pid = int(ch)
                        member = guild.get_member(pid) if guild else None
                        label = member.display_name if member else f"User {pid}"
                    except:
                        label = str(ch)
                else:
                    label = ch
                bet_lines.append(f"💰 {uname} bet {amt} on {label}")
            
            embed.add_field(name="📊 Bets", value="\n".join(bet_lines), inline=False)

        # Footer — clean, covers all winners
        if winner == "draw":
            embed.set_footer(text="🎮 Game has ended. Result: 🤝 Draw")
        elif winner == "ended":
            embed.set_footer(text="🎮 Game has ended.")
        elif isinstance(winner, int):
            member = guild.get_member(winner) if guild else None
            winner_name = member.display_name if member else f"User {winner}"
            embed.set_footer(text=f"🎮 Game has ended. Winner: {winner_name}")
        elif winner in ("Team A", "Team B"):
            embed.set_footer(text=f"🎮 Game has ended. Winner: {winner}")

        return embed

    async def get_odds(self, choice):
        ranks = [ (await get_player(p)).get("rank", 1000) for p in self.players ]

        if self.game_type == "singles" and len(ranks) >= 2:
            e1, e2 = ranks
            o1 = 1 / (1 + 10 ** ((e2 - e1) / 400))
            return o1 if choice in ("1", str(self.players[0])) else (1 - o1)

        elif self.game_type == "doubles" and len(ranks) >= 4:
            e1 = sum(ranks[:2]) / 2
            e2 = sum(ranks[2:]) / 2
            o1 = 1 / (1 + 10 ** ((e2 - e1) / 400))
            return o1 if choice.upper() == "A" else (1 - o1)

        elif self.game_type == "triples" and len(ranks) >= 3:
            exp = [10 ** (e / 400) for e in ranks]
            total = sum(exp)
            expected = [v / total for v in exp]
            for idx, p in enumerate(self.players):
                if choice in (str(idx + 1), str(p)):
                    return expected[idx]
            return 1 / len(ranks)

        elif self.game_type == "tournament":
            # Assume equal odds for now
            return 1 / len(ranks) if ranks else 0.5

        return 0.5

    async def add_bet(self, uid, uname, amount, choice, interaction):
        if uid in self.players:
            if self.game_type == "doubles":
                # Allow only if user is betting on their own team
                team_a = self.players[:2]
                team_b = self.players[2:]
                user_team = "A" if uid in team_a else "B"
                if normalize_team(choice) != user_team:
                    await self.safe_send(
                        interaction,
                        "❌ You can only bet on your **own team**.",
                        ephemeral=True
                    )
                    return False
            else:
                # Allow only if betting on self
                is_self_bet = (
                    choice == str(uid)
                    or choice == str(self.players.index(uid) + 1)
                )
                if not is_self_bet:
                    await self.safe_send(
                        interaction,
                        "❌ You can only bet on **yourself**.",
                        ephemeral=True
                    )
                    return False
        
        # Always store in the local bets
        if hasattr(self, "bets"):
            self.bets = [b for b in self.bets if b[0] != uid]
            self.bets.append((uid, uname, amount, choice))

        # ✅ Also store in manager if present
        if hasattr(self, "manager") and self.manager:
            self.manager.bets = [b for b in self.manager.bets if b[0] != uid]
            self.manager.bets.append((uid, uname, amount, choice))

        # ✅ Safe fallback for which message to update
        target_message = self.manager.message if hasattr(self, "manager") and self.manager else self.message

        # ✅ Use correct bets source
        bets = self.bets

        embed = await self.build_embed(
            target_message.guild,
            status="✅ Tournament full! Matches running — place your bets!" if not self.betting_closed else "🕐 Betting closed. Good luck!",
            bets=self.bets
        )
        await target_message.edit(embed=embed, view=self if not self.betting_closed else None)

        return True

    def get_bet_summary(self):
        if not bets:
            return "No bets placed yet."

        guild = self.message.guild if self.message else None
        lines = []

        for _, uname, amt, ch in bets:
            # Default
            label = str(ch)

            if self.game_type == "tournament":
                try:
                    pid = int(ch)
                    member = guild.get_member(pid) if guild else None
                    label = member.display_name if member else f"User {pid}"
                except:
                    label = str(ch)
            elif self.game_type == "doubles":
                label = f"Team {normalize_team(ch)}"
            else:
                try:
                    val = int(ch)
                    if (val - 1) < len(self.players):
                        pid = self.players[val - 1]
                        member = guild.get_member(pid) if guild else None
                        label = member.display_name if member else f"Player {val}"
                except:
                    pass

            lines.append(f"**{uname}** bet {amt} on **{label}**")

        return "\n".join(lines)

    @staticmethod
    async def safe_send(interaction: discord.Interaction, content=None, embed=None, view=None, **kwargs):
        kwargs = dict(content=content, embed=embed, **kwargs)
        if view is not None:
            kwargs["view"] = view
        if interaction.response.is_done():
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)


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

        # ✅ Validate amount
        try:
            amount = int(self.bet_amount.value.strip())
            if amount <= 0:
                raise ValueError()
        except ValueError:
            await self.safe_send(interaction, "❌ Invalid amount.", ephemeral=True)
            return

        # ✅ Get odds & payout
        odds_provider = getattr(self.game_view, "_embed_helper", self.game_view)
        odds = await odds_provider.get_odds(self.choice)
        payout = int(amount * (1 / odds)) if odds > 0 else amount

        # ✅ Deduct credits
        success = await deduct_credits_atomic(user_id, amount)
        if not success:
            await self.safe_send(interaction, "❌ Not enough credits.", ephemeral=True)
            return

        # ✅ Check if bet is allowed
        accepted = await self.game_view.add_bet(user_id, interaction.user.display_name, amount, self.choice, interaction)
        if not accepted:
            await add_credits_internal(user_id, amount)
            return

        # ✅ Log bet in DB
        await run_db(lambda: supabase.table("bets").insert({
            "player_id": str(user_id),
            "game_id": self.game_view.message.id,
            "choice": self.choice,
            "amount": amount,
            "payout": payout,
            "won": None
        }).execute())

        # ✅ Update embed
        await self.game_view.update_message()

        # ✅ Resolve choice name
        guild = self.game_view.message.guild if self.game_view.message else None
        players = getattr(self.game_view, "players", [])
        choice_name = str(self.choice)

        if self.choice.upper() in ["A", "B"]:
            choice_name = f"Team {self.choice.upper()}"
        else:
            try:
                idx = int(self.choice) - 1
                if 0 <= idx < len(players):
                    pid = players[idx]
                    member = guild.get_member(pid) if guild else None
                    choice_name = member.display_name if member else f"Player {self.choice}"
                else:
                    member = guild.get_member(int(self.choice)) if guild else None
                    choice_name = member.display_name if member else f"User {self.choice}"
            except:
                pass  # fallback

        # ✅ Final confirmation
        await self.safe_send(
            interaction,
            f"✅ Bet of **{amount}** on **{choice_name}** placed!\n"
            f"📊 Odds: {odds * 100:.1f}% | 💰 Payout: **{payout}**",
            ephemeral=True
        )


    async def safe_send(self, interaction: discord.Interaction, content: str, **kwargs):
        """Send safely: first response OR followup."""
        if interaction.response.is_done():
            await interaction.followup.send(content, **kwargs)
        else:
            await interaction.response.send_message(content, **kwargs)



async def update_leaderboard(bot, game_type="singles"):
    chan_id = await get_parameter(f"{game_type}_leaderboard_channel_id")
    msg_id = await get_parameter(f"{game_type}_leaderboard_message_id")
    if not chan_id or not msg_id:
        return

    chan = bot.get_channel(int(chan_id))
    if not chan:
        return

    try:
        msg = await chan.fetch_message(int(msg_id))
    except:
        return

    res = await run_db(lambda: supabase.table("players").select("*").execute())
    players = res.data or []
    players.sort(
        key=lambda p: int(p.get("stats", {}).get(game_type, {}).get("rank", 1000)),
        reverse=True
    )

    entries = [(p["id"], p) for p in players]
    view = LeaderboardView(entries, page_size=10, title=f"🏆 {game_type.capitalize()} Leaderboard", game_type=game_type)
    view.message = msg

    embed = discord.Embed(
        title=view.title,
        description=view.format_page(chan.guild),
        color=discord.Color.gold()
    )
    await msg.edit(embed=embed, view=view)


class LeaderboardView(discord.ui.View):
    def __init__(self, entries, page_size=10, title="🏆 Leaderboard", game_type="singles"):
        super().__init__(timeout=None)
        self.entries = entries
        self.page_size = page_size
        self.page = 0
        self.title = title
        self.message = None
        self.game_type = game_type  # ✅ dynamic!
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
            uid, stats = entry if isinstance(entry, tuple) else (entry.get("id"), entry)
            member = guild.get_member(int(uid))
            display = member.display_name if member else f"User {uid}"
            name = display[:18].ljust(18)

            # ✅ dynamic rank for this game type
            rank = stats.get("stats", {}).get(self.game_type, {}).get("rank", 1000)
            trophies = stats.get("stats", {}).get(self.game_type, {}).get("trophies", 0)
            credits = stats.get("credits", 0)

            badge = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else ""
            line = f"#{i:>2} {name} | 📈 {rank} {badge} | 🏆 {trophies:<3} | 💰 {credits:<4}"
            lines.append(line)

        if not lines:
            lines = ["No entries found."]

        page_info = f"Page {self.page + 1} of {max(1, (len(self.entries) + self.page_size - 1) // self.page_size)}"
        return f"```{chr(10).join(lines)}\n\n{page_info}```"

    async def update(self, interaction: discord.Interaction):
        self.update_buttons()
        embed = discord.Embed(
            title=self.title,
            description=self.format_page(interaction.guild),
            color=discord.Color.gold()
        )
        await interaction.response.edit_message(embed=embed, view=self)
        self.message = interaction.message  # update stored message in case you need it later


    class PreviousButton(discord.ui.Button):
        def __init__(self, view_obj):
            super().__init__(label="⬅ Previous", style=discord.ButtonStyle.secondary)
            self.view_obj = view_obj

        async def callback(self, interaction: discord.Interaction):
            self.view_obj.page = max(0, self.view_obj.page - 1)
            await self.view_obj.update(interaction)


    class NextButton(discord.ui.Button):
        def __init__(self, view_obj):
            super().__init__(label="Next ➡", style=discord.ButtonStyle.secondary)
            self.view_obj = view_obj

        async def callback(self, interaction: discord.Interaction):
            max_pages = (len(self.view_obj.entries) - 1) // self.view_obj.page_size
            self.view_obj.page = min(max_pages, self.view_obj.page + 1)
            await self.view_obj.update(interaction)


class SelectedGameInitButton(discord.ui.View):
    def __init__(self, bot, lobby_channel_id):
        super().__init__(timeout=None)
        self.bot = bot
        self.lobby_channel_id = lobby_channel_id

    @discord.ui.button(label="🎮 New Selected Game", style=discord.ButtonStyle.primary)
    async def create_selected_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        res = await run_db(lambda: supabase.table("courses").select("*").order("name").execute())
        all_courses = res.data or []

        if not all_courses:
            await interaction.response.send_message("⚠️ No courses found.", ephemeral=True)
            return

        # ✅ Callback after a course is selected
        async def on_course_selected(inter, course_id):
            course = next((c for c in all_courses if str(c["id"]) == course_id), None)
            if not course:
                await inter.response.send_message("❌ Course not found.", ephemeral=True)
                return

            course_name = course.get("name", "Unknown Course")
            course_image = course.get("image_url", "")
            room_name = await room_name_generator.get_unique_word()

            # 🕒 Use local timezone
            local_tz = zoneinfo.ZoneInfo("Europe/Copenhagen")
            now = datetime.now(tz=local_tz)
            timestamp = now.strftime("%H:%M")
            expire_ts = int((now + timedelta(minutes=15)).timestamp())

            embed = discord.Embed(
                title=f"🕹️ Selected Match Room: **{room_name.upper()}**",
                description=(
                    f"**Course:** `{course_name}`\n"
                    f"**Start Time:** `{timestamp}`\n"
                    f"⏳ *Expires <t:{expire_ts}:R>*\n"
                    f"\n👍 React if you're interested!"
                ),
                color=discord.Color.green()
            )
            if course_image:
                embed.set_image(url=course_image)

            lobby_channel = self.bot.get_channel(self.lobby_channel_id)
            msg = await lobby_channel.send(embed=embed)
            await msg.add_reaction("👍")
            await inter.response.edit_message(content="✅ Game created!", view=None)

        # ✅ Create paginated course picker
        view = PaginatedCourseView(all_courses, per_page=25, callback_fn=on_course_selected)
        await interaction.response.send_message("🧭 Select a course:", view=view, ephemeral=True)
        view.message = await interaction.original_response()


class PaginatedCourseView(discord.ui.View):
    def __init__(self, courses, per_page=25, callback_fn=None):
        super().__init__(timeout=120)
        self.courses = courses
        self.per_page = per_page
        self.page = 0
        self.message = None
        self.callback_fn = None  # ✅ define before update_children
        self.update_children()

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
            self.add_item(PaginatedCourseSelect(options, self, self.callback_fn))

        if self.page > 0:
            self.add_item(self.PrevButton(self))
        if end < len(self.courses):
            self.add_item(self.NextButton(self))

    async def update(self):
        self.update_children()
        if self.message:
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
    def __init__(self, options, parent_view, callback_fn=None):
        super().__init__(placeholder="Select a course", options=options)
        self.view_obj = parent_view
        self.callback_fn = callback_fn

    async def callback(self, interaction: discord.Interaction):
        course_id = self.values[0]
        selected = next((c for c in self.view_obj.courses if str(c["id"]) == course_id), None)
        if not selected:
            await interaction.response.send_message("❌ Course not found.", ephemeral=True)
            return

        if self.callback_fn:
            await self.callback_fn(interaction, course_id)
        else:
            # Default action (e.g. score submission)
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
    def __init__(self, bot, creator, max_players=16):
        self.bot = bot
        self.creator = creator
        self.players = [creator.id if hasattr(creator, "id") else creator]
        self.max_players = max_players
        self.bot = bot
        self.matches_completed_this_round = 0
        self.message = None           # the main lobby message in parent channel
        self.parent_channel = None    # the parent text channel
        self.current_matches = []
        self.winners = []
        self.round_players = []
        self.next_round_players = []
        self.started = False  

        self.bets = []  # ✅ NEW: store live bets (uid, uname, amount, choice)

        self.abandon_task = asyncio.create_task(self.abandon_if_not_filled())

        player_manager.activate(creator) 

    async def add_player(self, user):
        uid = user.id if hasattr(user, "id") else user
        if uid in self.players or len(self.players) >= self.max_players:
            return False
        self.players.append(uid)
        player_manager.activate(uid)
        return True

    async def abandon_if_not_filled(self):
        await asyncio.sleep(1000)
        if len(self.players) < self.max_players:
            embed = discord.Embed(
                title="❌ Tournament Abandoned",
                description="Not enough players joined in time.",
                color=discord.Color.red()
            )
            if self.message:
                await self.message.edit(embed=embed, view=None)

            for p in self.players:
                player_manager.deactivate(p)

            await start_new_game_button(self.parent_channel, "tournament")

    async def start_bracket(self, interaction):
        self.parent_channel = interaction.channel
        self.round_players = self.players.copy()
        random.shuffle(self.round_players)
        await self.run_round(interaction.guild)

    async def run_round(self, guild):
        self.matches_completed_this_round = 0
        players = self.round_players.copy()
        random.shuffle(players)

        self.current_matches = []
        self.winners = []
        self.next_round_players = []

        res = await run_db(lambda: supabase.table("courses").select("*").execute())
        chosen = random.choice(res.data or [{}])
        course_id = chosen.get("id")
        course_name = chosen.get("name", "Unknown")
        course_image = chosen.get("image_url", "")

        for i in range(0, len(players), 2):
            if i + 1 < len(players):
                p1 = players[i]
                p2 = players[i + 1]

                room_name = await room_name_generator.get_unique_word()

                try:
                    match_thread = await self.parent_channel.create_thread(
                        name=f"Match-{room_name}",
                        type=discord.ChannelType.private_thread,
                        invitable=False
                    )
                except discord.Forbidden:
                    print(f"❌ Missing permission to create thread in #{self.parent_channel}")
                    continue
                except discord.HTTPException as e:
                    print(f"❌ Failed to create thread: {e}")
                    continue

                for pid in [p1, p2]:
                    try:
                        member = guild.get_member(pid) or await guild.fetch_member(pid)
                        await match_thread.add_user(member)
                    except discord.NotFound:
                        print(f"[warn] Could not fetch or add user {pid}")

                room_view = RoomView(
                    bot=bot,
                    guild=guild,
                    players=[p1, p2],
                    game_type="singles",
                    room_name=room_name,
                    course_name=course_name,
                    course_id=course_id,
                    max_players=2
                )
                room_view.course_image = course_image
                room_view.guild = guild
                room_view.on_tournament_complete = self.match_complete
                room_view.channel = match_thread

                embed = await room_view.build_room_embed()
                embed.title = f"Room: {room_name}"
                embed.description = f"Course: {course_name}"
                room_view.lobby_embed = embed

                mentions = f"<@{p1}> <@{p2}>"

                try:
                    msg = await match_thread.send(
                        content=f"{mentions}\n🏆 This match is part of the tournament!",
                        embed=embed,
                        view=room_view
                    )
                except Exception as e:
                    print(f"❌ Failed to send match embed in thread: {e}")
                    continue

                room_view.message = msg
                room_view.channel = match_thread

                self.current_matches.append(room_view)

            else:
                self.next_round_players.append(players[i])

    async def match_complete(self, winner_id):
        self.matches_completed_this_round += 1
        self.winners.append(winner_id)
        self.next_round_players.append(winner_id)

        pending_games["tournament"] = None

        # ✅ Find the loser in the current match pair
        loser_id = None
        for match in self.current_matches:
            if winner_id in match.players:
                loser_id = next((p for p in match.players if p != winner_id), None)
                break

        # ✅ SAFEGUARD
        if loser_id is None:
            print(f"[ELO ERROR] Could not determine loser for winner {winner_id}. Skipping ELO update.")
        else:
            # Only update ELO if both players are known
            await update_elo_pair_and_save(
                winner_id,
                loser_id,
                winner=1,
                game_type="tournaments"
            )

        # ✅ Deactivate loser for tournament room tracking
        if loser_id:
            player_manager.deactivate(loser_id)

        # ✅ Refresh leaderboard
        await update_leaderboard(self.bot, "tournaments")

        # ✅ Check if all matches for this round are done
        if self.matches_completed_this_round >= len(self.current_matches):
            if len(self.next_round_players) == 1:
                # ✅ Final champion found
                champ = self.next_round_players[0]
                player_manager.deactivate(champ)

                # ✅ Process bets for the whole tournament
                for uid, uname, amount, choice in self.bets:
                    try:
                        won = int(choice) == champ
                    except:
                        won = False

                    await run_db(lambda: supabase
                        .table("bets")
                        .update({"won": won})
                        .eq("player_id", uid)
                        .eq("game_id", self.message.id)
                        .eq("choice", choice)
                        .execute()
                    )

                    if won:
                        odds = 0.5  # Optional: store real odds per bet
                        payout = int(amount / odds)
                        await add_credits_internal(uid, payout)
                        print(f"💰 {uname} won! Payout: {payout}")
                    else:
                        print(f"❌ {uname} lost {amount}")

                # ✅ Build final champion embed
                final_embed = discord.Embed(
                    title="🏆 Tournament Results",
                    description=f"**Champion:** <@{champ}>",
                    color=discord.Color.gold()
                )
                final_embed.set_footer(text="Thanks for playing!")

                if self.message:
                    await self.message.edit(embed=final_embed, view=None)

                print(f"🏆 Tournament completed. Champion: {champ}")

            else:
                # ✅ More rounds remain → start next round
                self.round_players = self.next_round_players.copy()
                self.next_round_players = []
                await self.run_round(self.parent_channel.guild)



class TournamentLobbyView(discord.ui.View):
    def __init__(self, manager, creator, max_players, parent_channel, status=None):
        super().__init__(timeout=None)
        self.manager = manager
        self.creator = creator
        self.max_players = max_players
        self.game_type = "tournament"
        self.betting_task = None
        self.abandon_task = None 
        self.message = None
        self.betting_closed = False
        self.bets = []
        self.status = None
        self.parent_channel = parent_channel

        # ✅ Robust: always store a valid int ID in players list
        creator_id = creator.id if hasattr(creator, "id") else creator
        self.players = [creator_id]

        # Join button
        self.join_button = discord.ui.Button(label="Join Tournament", style=discord.ButtonStyle.success)
        self.join_button.callback = self.join
        self.add_item(self.join_button)

        # ✅ static Leave button:
        self.add_item(LeaveGameButton(self))

        # ✅ FIXED: pass channel!
        self._embed_helper = GameView(
            game_type="tournament",
            creator=creator_id,
            max_players=max_players,
            channel=self.parent_channel
        )
        self._embed_helper.players = self.players
        self._embed_helper.bets = self.bets

    def cancel_betting_task(self):
        if self.betting_task:
            self.betting_task.cancel()
            self.betting_task = None

    def cancel_abandon_task(self):
        if hasattr(self, "abandon_game") and self.abandon_task:
            self.abandon_task.cancel()
            self.abandon_task = None

    async def abandon_game(self, reason):
        self.cancel_abandon_task()
        self.cancel_betting_task()

        pending_games[self.game_type] = None

        for p in self.players:
            player_manager.deactivate(p)

        embed = discord.Embed(
            title="❌ Game Abandoned",
            description=reason,
            color=discord.Color.red()
        )

        if self.message:
            try:
                await self.message.edit(embed=embed, view=None)
            except:
                pass

        self.message = None

        # ✅ Call the same flow as /init_...
        await start_new_game_button(self.parent_channel, "tournament")

        print(f"[abandon_game] New start posted for {self.game_type} in #{self.parent_channel.name}")

    async def join(self, interaction: discord.Interaction):
        uid = interaction.user.id

        if uid in self.players:
            await interaction.response.send_message("✅ You are already in the tournament.", ephemeral=True)
            return

        if len(self.players) >= self.max_players:
            await interaction.response.send_message("🚫 Tournament is full.", ephemeral=True)
            return

        if player_manager.is_active(uid):
            await interaction.response.send_message("🚫 You are already in another active match.", ephemeral=True)
            return

        # ✅ Append to both
        self.players.append(uid)
        self.manager.players.append(uid)
        player_manager.activate(uid)

        await self.update_message()
        await interaction.response.send_message("✅ You joined the tournament!", ephemeral=True)

        print(f"👥 Players: {self.players} / {self.max_players}")
        print(f"📦 Manager players: {self.manager.players}")

        if len(self.players) == self.max_players and not getattr(self.manager, "started", False):
            self.manager.started = True
            pending_games["tournament"] = None

            self.clear_items()
            if not any(isinstance(item, BettingButtonDropdown) for item in self.children):
                self.add_item(BettingButtonDropdown(self))

            await self.update_message(status="✅ Match is full. Place your bets!")

            if self.abandon_task:
                self.abandon_task.cancel()

            print("🚀 Starting tournament bracket...")
            await self.manager.start_bracket(interaction)

            # ✅ Immediately post a new tournament button
            await start_new_game_button(self.parent_channel, "tournament")


    async def abandon_if_not_filled(self):
        try:
            await asyncio.sleep(1000)
            if self.started:
                return  # ✅ Game already started

            if len(self.players) < self.max_players:
                await self.view.abandon_game("⏰ Tournament timed out.")
        except asyncio.CancelledError:
            pass  # ✅ clean cancel

    async def build_embed(self, guild, winner=None, no_image=True, status=None, bets=None):
        self._embed_helper.players = self.players
        self._embed_helper.bets = self.bets
        self._embed_helper.betting_closed = self.betting_closed
        final_status = status if status is not None else self.status

        if bets is None:
            bets = self.manager.bets

        return await self._embed_helper.build_embed(
            guild,
            winner=winner,
            no_image=no_image,
            status=final_status,
            bets=bets
        )

    async def update_message(self, status=None):
        if self.message:
            embed = await self.build_embed(self.message.guild, status=status)
            await self.message.edit(embed=embed, view=self)

    async def add_bet(self, uid, uname, amount, choice, interaction):
        # ✅ Block players from betting on others in their own tournament
        if uid in self.players:
            is_self_bet = (
                choice == str(uid)
                or choice == str(self.players.index(uid) + 1)
            )
            if not is_self_bet:
                await interaction.response.send_message(
                    "❌ You can only bet on **yourself**.",
                    ephemeral=True
                )
                return False

        # ✅ Deduplicate in tournament bets
        self.manager.bets = [b for b in self.manager.bets if b[0] != uid]
        self.manager.bets.append((uid, uname, amount, choice))

        # ✅ Re-render updated embed
        if self.message:
            embed = await self.build_embed(self.message.guild)
            await self.message.edit(embed=embed, view=self)

        return True


class PlayerCountModal(discord.ui.Modal, title="Select Tournament Size"):
    def __init__(self, parent_channel, creator, view):
        super().__init__()
        self.parent_channel = parent_channel
        self.creator = creator
        self.view = view

        self.player_count = discord.ui.TextInput(
            label="Number of players (even number)",
            placeholder="E.g. 4, 8, 16",
            required=True
        )
        self.add_item(self.player_count)

    async def build_embed(self, *args, **kwargs):
        return await self._embed_helper.build_embed(*args, **kwargs)

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

        if player_manager.is_active(self.creator.id):
            await interaction.response.send_message(
                "🚫 You are already in a game or tournament. Finish it first.",
                ephemeral=True
            )
            return

        player_manager.activate(self.creator.id)

        await interaction.response.defer(ephemeral=True)

        # ✅ Always provide parent_channel up-front:
        manager = TournamentManager(bot=bot, creator=self.creator.id, max_players=count)
        manager.parent_channel = self.parent_channel

        interaction.client.tournaments[self.parent_channel.id] = manager

        if IS_TEST_MODE:
            for pid in TEST_PLAYER_IDS:
                if pid not in manager.players and len(manager.players) < manager.max_players:
                    manager.players.append(pid)

        # ✅ FIX: pass parent_channel explicitly!
        view = TournamentLobbyView(
            manager,
            creator=self.creator,
            max_players=count,
            parent_channel=self.parent_channel 
        )
        manager.view = view
        view.players = manager.players.copy()  # sync test players if any

        view.status = "✅ Tournament full! Matches running — place your bets!" if IS_TEST_MODE else None

        embed = await view.build_embed(interaction.guild, no_image=True)
        manager.message = await interaction.channel.send(embed=embed, view=view)
        view.message = manager.message

        if len(view.players) == view.max_players:
            view.clear_items()
            view.add_item(BettingButtonDropdown(view))
            await view.update_message()

            if manager.abandon_task:
                manager.abandon_task.cancel()

            manager.started = True

            await manager.start_bracket(interaction)

            #await asyncio.sleep(120)
            #view.betting_closed = True
            #view.clear_items()
            #await view.update_message()

        await interaction.followup.send(
            f"✅ Tournament created for **{count} players!**",
            ephemeral=True
        )

        #await start_new_game_button(interaction.channel, "tournament")


@tree.command(name="init_tournament")
async def init_tournament(interaction: discord.Interaction):
    """Creates a tournament game lobby with the start button"""

    print("[init_tournament] Defer interaction...")
    await interaction.response.defer(ephemeral=True)

    print("[init_tournament] Checking for existing game or button...")
    if pending_games.get("tournament") or any(k[0] == interaction.channel.id for k in start_buttons):
        print("[init_tournament] Found existing game/button, sending followup...")
        await interaction.followup.send(
            "⚠️ A tournament game is already pending or a button is active here.",
            ephemeral=True
        )
        return

    max_players = 16

    print("[init_tournament] Calling start_new_game_button...")
    # ✅ Ensure this never takes 3+ seconds; if it might, break it up:
    await start_new_game_button(interaction.channel, "tournament", max_players=max_players)

    print("[init_tournament] Sending success followup...")
    await interaction.followup.send(
        "✅ Tournament game button posted and ready for players to join!",
        ephemeral=True
    )



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

    print("[init_doubles] Defer interaction...")
    await interaction.response.defer(ephemeral=True)

    print("[init_doubles] Checking for existing game or button...")
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

    print("[init_doubles] Defer interaction...")
    await interaction.response.defer(ephemeral=True)

    print("[init_doubles] Checking for existing game or button...")
    if pending_games.get("doubles") or any(k[0] == interaction.channel.id for k in start_buttons):
        print("[init_doubles] Found existing game/button, sending followup...")
        await interaction.followup.send(
            "⚠️ A doubles game is already pending or a button is active here.",
            ephemeral=True
        )
        return

    max_players = 4

    print("[init_doubles] Calling start_new_game_button...")
    # ✅ Ensure this never takes 3+ seconds; if it might, break it up:
    await start_new_game_button(interaction.channel, "doubles", max_players=max_players)

    print("[init_doubles] Sending success followup...")
    await interaction.followup.send(
        "✅ Doubles game button posted and ready for players to join!",
        ephemeral=True
    )

@tree.command(name="init_triples")
async def init_triples(interaction: discord.Interaction):
    """Creates a triples game lobby with the start button"""

    print("[init_triples] Defer interaction...")
    await interaction.response.defer(ephemeral=True)

    print("[init_triples] Checking for existing game or button...")
    if pending_games.get("triples") or any(k[0] == interaction.channel.id for k in start_buttons):
        print("[init_singles] Found existing game/button, sending followup...")
        await interaction.followup.send(
            "⚠️ A triples game is already pending or a button is active here.",
            ephemeral=True
        )
        return

    max_players = 3

    print("[init_triples] Calling start_new_game_button...")
    # ✅ Ensure this never takes 3+ seconds; if it might, break it up:
    await start_new_game_button(interaction.channel, "triples", max_players=max_players)

    print("[init_triples] Sending success followup...")
    await interaction.followup.send(
        "✅ Triples game button posted and ready for players to join!",
        ephemeral=True
    )



@tree.command(
    name="admin_leaderboard",
    description="Admin: Show the leaderboard for a specific game type"
)
@app_commands.describe(
    game_type="Which game type to show (singles, doubles, triples, tournaments)"
)
@app_commands.check(is_admin)  # ✅ only admins can run
async def admin_leaderboard(
    interaction: discord.Interaction,
    game_type: str
):
    allowed = ["singles", "doubles", "triples", "tournaments"]
    if game_type not in allowed:
        await interaction.response.send_message(
            f"❌ Invalid game type. Use: {', '.join(allowed)}",
            ephemeral=True
        )
        return

    await interaction.response.defer()  # ✅ public defer

    # ✅ Fetch all players
    res = await run_db(lambda: supabase.table("players").select("*").execute())
    players = res.data or []

    # ✅ Sort numerically by selected game type rank
    players.sort(
        key=lambda p: int(p.get("stats", {}).get(game_type, {}).get("rank", 1000)),
        reverse=True
    )

    if not players:
        await interaction.followup.send(
            "📭 No players found.",
            ephemeral=True  # error stays private
        )
        return

    # ✅ Format entries for the view
    entries = [(p["id"], p) for p in players]

    # ✅ Create view with game_type
    view = LeaderboardView(
        entries,
        page_size=10,
        title=f"🏆 {game_type.capitalize()} Leaderboard",
        game_type=game_type
    )

    # ✅ Send the leaderboard PUBLICLY in channel
    embed = discord.Embed(
        title=view.title,
        description=view.format_page(interaction.guild),
        color=discord.Color.gold()
    )
    await interaction.followup.send(embed=embed, view=view)
    view.message = await interaction.original_response()

    # ✅ Store channel/message IDs PER game type for auto-update
    await set_parameter(f"{game_type}_leaderboard_channel_id", str(interaction.channel.id))
    await set_parameter(f"{game_type}_leaderboard_message_id", str(view.message.id))


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
    description="Show your stats (or another user's)."
)
async def stats(interaction: discord.Interaction, user: discord.User = None, dm: bool = False):
    await interaction.response.defer(ephemeral=True)

    target_user = user or interaction.user

    # ✅ Fetch player row
    res = await run_db(
        lambda: supabase.table("players").select("*").eq("id", str(target_user.id)).single().execute()
    )
    player = res.data or {}

    credits = player.get("credits", 1000)
    stats_data = player.get("stats", {})

    # ✅ Build sections for each game type
    blocks = []
    for game_type in ("singles", "doubles", "triples", "tournaments"):
        stats = stats_data.get(game_type, {})
        rank = stats.get("rank", 1000)
        trophies = stats.get("trophies", 0)
        games = stats.get("games_played", 0)
        wins = stats.get("wins", 0)
        losses = stats.get("losses", 0)
        draws = stats.get("draws", 0)
        streak = stats.get("current_streak", 0)
        best_streak = stats.get("best_streak", 0)

        block = [
            f"{'📈 Rank':<20}: {rank}",
            f"{'🏆 Trophies':<20}: {trophies}",
            f"{'🎮 Games Played':<20}: {games}",
            f"{'✅ Wins':<20}: {wins}",
            f"{'❌ Losses':<20}: {losses}",
            f"{'➖ Draws':<20}: {draws}",
            f"{'🔥 Current Streak':<20}: {streak}",
            f"{'🏅 Best Streak':<20}: {best_streak}"
        ]
        blocks.append(f"**{game_type.title()} Stats**\n```" + "\n".join(block) + "```")

    # ✅ Add global credits at top
    blocks.insert(0, f"**💰 Balls:** `{credits}`")

    # ✅ Build embed with all sections
    embed = discord.Embed(
        title=f"📊 Stats for {target_user.display_name}",
        description="\n\n".join(blocks),
        color=discord.Color.blue()
    )

    # ✅ Add recent bets (unchanged)
    bets = await run_db(
        lambda: supabase.table("bets")
        .select("id,won,payout,amount,choice")
        .eq("player_id", str(target_user.id))
        .order("id", desc=True)
        .limit(5)
        .execute()
    )
    all_bets = await run_db(
        lambda: supabase.table("bets")
        .select("won,payout,amount")
        .eq("player_id", str(target_user.id))
        .execute()
    )

    total_bets = len(all_bets.data or [])
    bets_won = sum(1 for b in all_bets.data if b.get("won") is True)
    bets_lost = sum(1 for b in all_bets.data if b.get("won") is False)
    net_gain = sum(b.get("payout", 0) - b.get("amount", 0) for b in all_bets.data if b.get("won") is not None)

    bet_stats = [
        f"{'🪙 Total Bets':<20}: {total_bets}",
        f"{'✅ Bets Won':<20}: {bets_won}",
        f"{'❌ Bets Lost':<20}: {bets_lost}",
        f"{'💸 Net Gain/Loss':<20}: {net_gain:+}"
    ]

    embed.add_field(
        name="🎰 Betting Stats",
        value="```" + "\n".join(bet_stats) + "```",
        inline=False
    )

    if bets.data:
        recent_lines = []
        for b in bets.data:
            won = b.get("won")
            choice = b.get("choice", "?")
            amount = b.get("amount", 0)
            payout = b.get("payout", 0)

            guild = interaction.guild
            choice_label = str(choice)

            if str(choice).upper() in ("A", "B"):
                choice_label = f"Team {choice.upper()}"
            else:
                try:
                    # Attempt to resolve as Discord user ID
                    member = guild.get_member(int(choice)) if guild else None
                    choice_label = member.display_name if member else f"User {choice}"
                except:
                    pass

            if won is True:
                line = f"✅ Won  {amount:<5} on {choice_label:<8} → Payout {payout}"
            elif won is False:
                line = f"❌ Lost {amount:<5} on {choice_label:<8} → Payout 0"
            else:
                line = f"⚪️ Draw {amount:<5} on {choice_label:<8} → Refunded"

            recent_lines.append(line)

        embed.add_field(
            name="🗓️ Recent Bets",
            value="```" + "\n".join(recent_lines) + "```",
            inline=False
        )

    # ✅ Send DM or ephemeral
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
    name="clear_pending_games",
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



@tree.command(
    name="handicap_index",
    description="Calculate your current handicap index (average of your best scores)"
)
async def handicap_index(interaction: discord.Interaction, user: discord.User = None):
    await interaction.response.defer(ephemeral=True)

    target = user or interaction.user

    res = await run_db(lambda: supabase
        .table("handicaps")
        .select("handicap")
        .eq("player_id", str(target.id))
        .execute()
    )

    differentials = sorted([row["handicap"] for row in res.data or []])
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
    await interaction.response.defer(ephemeral=True)
    target = user or interaction.user

    try:
        res = await run_db(lambda: supabase
            .table("handicaps")
            .select("score,handicap,course_id,courses(name)")
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
        course_name = h["courses"]["name"]
        score = h["score"]
        differential = h.get("handicap", "N/A")

        embed.add_field(
            name=course_name,
            value=f"Score: **{score}**\nDifferential: **{differential:.2f}**" if isinstance(differential, (int, float)) else f"Score: **{score}**\nDifferential: **{differential}**",
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
        .select("player_id, handicap")
        .execute()
    )

    if not res.data:
        await interaction.followup.send("❌ No handicap data found.", ephemeral=True)
        return    

    grouped = defaultdict(list)
    for row in res.data:
        try:
            hcp = float(row["handicap"])
            grouped[row["player_id"]].append(hcp)
        except (TypeError, ValueError):
            continue  # skip invalid

    leaderboard = []
    for pid, diffs in grouped.items():
        diffs = sorted(diffs)
        count = min(len(diffs), 8)
        if count > 0:
            index = round(sum(diffs[:count]) / count, 1)
            leaderboard.append((pid, index))

    # 3️⃣ Sort by index ascending (lower is better)
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
        name = fixed_width_name(name)
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


# ✅ Register directly on your bot instance (no separate Cog needed)
@app_commands.command(
    name="update_roles",
    description="Assign specified roles to all existing server members"
)
@app_commands.describe(
    role_names="Comma-separated list of role names to assign"
)
async def update_roles(interaction: discord.Interaction, role_names: str):
    guild = interaction.guild

    # Parse role names
    names = [r.strip() for r in role_names.split(",")]

    # Find roles
    roles_to_add = []
    for name in names:
        role = discord.utils.get(guild.roles, name=name)
        if not role:
            await interaction.response.send_message(
                f"❌ Role `{name}` not found.",
                ephemeral=True
            )
            return
        roles_to_add.append(role)

    await interaction.response.send_message(
        f"⏳ Assigning roles `{', '.join([r.name for r in roles_to_add])}` to all existing members...",
        ephemeral=True
    )

    count = 0
    for member in guild.members:
        if member.bot:
            continue  # Optional: skip bots
        try:
            await member.add_roles(*roles_to_add, reason="Bulk role update via slash command")
            count += 1
        except Exception as e:
            print(f"Error adding roles to {member}: {e}")

    await interaction.followup.send(
        f"✅ Done! Updated roles for **{count}** existing members.",
        ephemeral=True
    )

# ✅ Register the command with your bot
bot.tree.add_command(update_roles)


@bot.event
async def on_member_join(member):
    # List of role names you want to auto-assign
    role_names = ["singles", "doubles", "triples, quick-tournament"]

    roles = []
    for name in role_names:
        role = discord.utils.get(member.guild.roles, name=name)
        if role:
            roles.append(role)

    if roles:
        await member.add_roles(*roles)

async def save_game_state(manager, view, room_view):
    """Store the current active game in Supabase for resilience."""

    print("[save_game_state] Raw players:", view.players)
    print("[save_game_state] Raw bets:", view.bets)

    players_clean = [p.id if hasattr(p, "id") else int(p) for p in view.players]

    bets_as_dicts = [
        {"uid": int(uid), "uname": str(uname), "amount": int(amount), "choice": str(choice)}
        for (uid, uname, amount, choice) in view.bets
    ]

    def write_state():
        data = {
            "game_id": str(view.message.id),
            "game_type": view.game_type,
            "parent_channel_id": str(view.channel.id),
            "thread_id": str(room_view.channel.id) if room_view else str(view.message.channel.id),
            "room_message_id": str(room_view.message.id) if room_view else None,
            "players": players_clean,
            "bets": bets_as_dicts,
            "max_players": int(view.max_players),
            "started": True,
        }
        print("[save_game_state] Payload:", json.dumps(data, indent=2))
        res = supabase.table("active_games").upsert(data).execute()
        print("[save_game_state] Supabase response:", res)
        return res

    await run_db(write_state)


async def restore_active_games(bot):
    """Load saved games from Supabase and rebuild Tournament managers + lobby + RoomViews."""

    result = await run_db(lambda: supabase.table("active_games").select("*").execute())
    active_games = result.data

    if not active_games:
        print("[restore] No active games to restore.")
        return

    print(f"[restore] Found {len(active_games)} games to restore.")

    for g in active_games:
        try:
            guild = bot.guilds[0]  # Adjust if multi-guild

            # ✅ Parent text channel (for lobby message)
            parent_channel = guild.get_channel(int(g["parent_channel_id"]))
            if not parent_channel:
                print(f"[restore] ❌ Parent channel {g['parent_channel_id']} not found. Skipping.")
                continue

            # ✅ Room sub-thread
            room_thread = await bot.fetch_channel(int(g["thread_id"]))
            if not room_thread:
                print(f"[restore] ❌ Room thread {g['thread_id']} not found. Skipping.")
                continue

            # ✅ Lobby message lives in parent channel
            lobby_message = await parent_channel.fetch_message(int(g["game_id"]))

            # ✅ Room message inside Room thread
            room_message_id = g.get("room_message_id")
            if not room_message_id:
                print(f"[restore] ❌ No room_message_id for game {g['game_id']}. Skipping RoomView.")
                continue

            try:
                room_message = await room_thread.fetch_message(int(room_message_id))
            except discord.NotFound:
                print(f"[restore] ⚠️ Room message {room_message_id} not found. Skipping RoomView restore.")
                room_message = None

            # ✅ Clean player IDs
            players = [int(pid) for pid in g["players"]]

            # ✅ Robust: Pick valid creator using get_member first, then fetch_user, then fallback to raw ID
            creator = None
            for pid in players:
                member = guild.get_member(pid)
                if member:
                    creator = member
                    break
                try:
                    creator = await bot.fetch_user(pid)
                    break
                except discord.NotFound:
                    continue

            if creator is None:
                # ⚠️ Fallback to raw ID if no user object could be resolved
                creator = players[0]
                print(f"[restore] ⚠️ No valid Discord User found. Using raw ID: {creator}")

            # ✅ Rebuild TournamentManager
            # Store only ID inside the manager (safe)
            manager = TournamentManager(
                bot=bot,
                creator=creator.id if hasattr(creator, "id") else creator,
                max_players=g["max_players"]
            )
            manager.started = g["started"]
            manager.parent_channel = parent_channel
            manager.bets = g.get("bets", [])

            # ✅ Rebuild TournamentLobbyView (lobby)
            lobby_view = TournamentLobbyView(
                manager=manager,
                creator=creator,  # could be a Member, User, or int
                max_players=g["max_players"],
                parent_channel=parent_channel
            )
            lobby_view.players = players
            lobby_view.bets = manager.bets
            lobby_view.message = lobby_message
            manager.view = lobby_view

            # ✅ Update lobby embed + buttons
            lobby_embed = await lobby_view.build_embed(guild)
            await lobby_message.edit(embed=lobby_embed, view=lobby_view)

            # ✅ Rebuild RoomView if Room message exists
            if room_message:
                room_view = RoomView(
                    bot=bot,
                    guild=guild,
                    players=players,
                    game_type=g["game_type"],
                    room_name="Restored Room",
                    lobby_message=lobby_message,
                    lobby_embed=lobby_embed,
                    game_view=lobby_view,
                    course_name=g.get("course_name"),
                    course_id=g.get("course_id"),
                    max_players=g["max_players"]
                )
                room_view.channel = room_thread
                room_view.message = room_message

                room_embed = await room_view.build_room_embed(guild)
                await room_message.edit(embed=room_embed, view=room_view)

                # ✅ Track RoomView
                if not hasattr(bot, "rooms"):
                    bot.rooms = {}
                bot.rooms[room_thread.id] = room_view

                print(f"[restore] ✅ Restored RoomView in thread #{room_thread.name}")

            # ✅ Restart betting phase if needed
            if hasattr(lobby_view, "start_betting_phase") and not lobby_view.betting_closed:
                await lobby_view.start_betting_phase()

            # ✅ Track TournamentManager
            if not hasattr(bot, "tournaments"):
                bot.tournaments = {}
            bot.tournaments[parent_channel.id] = manager

            print(f"[restore] ✅ Restored lobby + manager for parent channel #{parent_channel.name}")

        except Exception as e:
            print(f"[restore] ❌ Error restoring game {g.get('game_id')}: {e}")

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

@tree.command(name="init_selected", description="Post a button to create a selected course game")
async def init_selected(interaction: discord.Interaction):
    """Post a button to start a selected course game."""
    await interaction.response.send_message(
        "🎯 Click below to start a **selected course** game:",
        view=SelectedGameInitButton(bot, 1388048930503397506),
        ephemeral=True
    )


@bot.event
async def on_ready():
    await tree.sync()
    print(f"✅ Logged in as {bot.user}")
    #await restore_active_games(bot)
    bot.loop.create_task(hourly_room_announcer(bot, 1388042320061927434))

    rows = await load_pending_games()
    for row in rows:
        game_type = row["game_type"]
        players = row["players"]
        pending_games[game_type] = {"players": players}

    print(f"✅ Loaded pending games into RAM for checks: {pending_games}")


bot.run(os.getenv("DISCORD_BOT_TOKEN"))
