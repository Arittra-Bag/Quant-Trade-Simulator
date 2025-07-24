def calculate_fees(quantity, fee_tier):
    """
    Calculate expected fees based on the exchange fee tier and order quantity.
    
    This function implements a rule-based fee model based on OKX's tier structure.
    The fee rates are based on OKX's published fee schedule and represent the taker
    fees for different VIP levels.
    
    Fee Structure:
    - Tier 1: 0.08% (0.0008) - Standard/VIP 0 fee level
    - Tier 2: 0.07% (0.0007) - VIP 1 fee level
    - Tier 3: 0.06% (0.0006) - VIP 2 fee level
    
    For market orders, taker fees apply. For limit orders that provide liquidity,
    maker fees would be lower, but are not implemented here as we're focusing on
    market orders.
    
    Args:
        quantity (float): Order quantity in USD
        fee_tier (str): Selected fee tier ("Tier 1", "Tier 2", or "Tier 3")
    
    Returns:
        float: Expected fee amount in USD
    
    Example:
        >>> calculate_fees(1000, "Tier 1")
        0.8  # 0.08% of 1000 USD
    """
    try:
        # Define fee rates for each tier
        tier_rates = {
            "Tier 1": 0.0008,  # 0.08%
            "Tier 2": 0.0007,  # 0.07%
            "Tier 3": 0.0006   # 0.06%
        }
        
        # Get rate for the selected tier, default to Tier 1 if not found
        rate = tier_rates.get(fee_tier, 0.0008)
        
        # Calculate fee amount
        fee_amount = float(quantity) * rate
        
        return fee_amount
    except Exception as e:
        # Handle errors gracefully and return 0 as a safe default
        print(f"Error calculating fees: {e}")
        return 0.0 