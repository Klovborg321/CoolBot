@bot.tree.command(name="clear_chat", description="Admin: Delete all messages in this channel (last 14 days only)")
@discord.app_commands.checks.has_permissions(administrator=True)
async def clear_chat(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    channel = interaction.channel

    def not_bot(msg):
        return not msg.pinned  # Optional: skip pinned messages

    try:
        deleted = await channel.purge(limit=1000, check=not_bot, bulk=True)
        # ✅ NEW: Remove any stale button for this channel
        for key in list(start_buttons.keys()):
            if key[0] == channel.id:
                del start_buttons[key]

        await interaction.followup.send(f"🧹 Cleared {len(deleted)} messages.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"⚠️ Failed to clear messages: {e}", ephemeral=True)
