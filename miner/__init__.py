# Miner package

def build_hf_repo_id(config, round_num: int) -> str:
    """Build a Hugging Face repo ID matching the public protocol format.
    Format: {hf_username}/pi05-{hotkey_ss58_last12}
    
    The hotkey SS58 address is resolved automatically from the wallet
    if not already configured in config.
    """
    username = getattr(config, "hf_username", "") or "miner"
    
    # Try config first
    hotkey_ss58 = getattr(config, "hotkey_ss58", "")
    
    # If not configured, resolve from wallet
    if not hotkey_ss58:
        try:
            from utils.chain import get_wallet
            wallet_path = getattr(config, "wallet_path", "")
            coldkey = getattr(config, "coldkey", "default")
            hotkey_name = getattr(config, "hotkey", "default")
            password = getattr(config, "wallet_password", "")
            wallet = get_wallet(coldkey, hotkey_name, wallet_path, password)
            hotkey_ss58 = wallet.hotkey.ss58_address
        except Exception as e:
            import logging
            logging.getLogger("miner").warning(
                f"Failed to resolve hotkey_ss58 from wallet: {e}, using 'miner' fallback"
            )
            hotkey_ss58 = "miner"
    
    # Use last 12 chars of SS58 address
    hotkey_short = hotkey_ss58[-12:] if len(hotkey_ss58) >= 12 else hotkey_ss58
    return f"{username}/pi05-{hotkey_short}"
