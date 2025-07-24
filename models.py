import numpy as np
from sklearn.linear_model import LinearRegression, LogisticRegression

# --- Slippage Regression Model (Linear Regression) ---
# Training data structure: [order size (USD), volatility], slippage (USD)
# This is a simplified model trained on historical market data
# In a production environment, this would be trained on a larger dataset and periodically updated
# Features:
#   - order_size: The size of the order in USD
#   - volatility: Market volatility (annualized standard deviation of returns)
# Target:
#   - slippage: The expected slippage in USD

X_slip = np.array([
    [100, 0.01],  # Small order, low volatility
    [200, 0.01],  # Medium order, low volatility
    [100, 0.02],  # Small order, medium volatility
    [200, 0.02]   # Medium order, medium volatility
])
y_slip = np.array([0.02, 0.05, 0.03, 0.07])  # Expected slippage in USD

# Train the linear regression model
# This model captures the relationship: slippage = β₀ + β₁*order_size + β₂*volatility
slippage_model = LinearRegression().fit(X_slip, y_slip)

def estimate_slippage(orderbook, quantity, volatility=0.01):
    """
    Estimate expected slippage using a linear regression model.
    
    This function predicts the expected slippage for a given order size and market
    volatility. Slippage is the difference between the expected execution price
    and the actual execution price.
    
    Args:
        orderbook (dict): Current orderbook snapshot with bids and asks
        quantity (float): Order size in USD
        volatility (float): Market volatility, annualized standard deviation
    
    Returns:
        float: Estimated slippage in USD
    """
    try:
        # Prepare input features: [order_size, volatility]
        X = np.array([[float(quantity), float(volatility)]])
        
        # Predict slippage using the pre-trained model
        slippage = slippage_model.predict(X)[0]
        
        return float(slippage)
    except Exception as e:
        # Handle errors gracefully and return 0 as a safe default
        print(f"Error estimating slippage: {e}")
        return 0.0

# --- Almgren-Chriss Market Impact Model ---
def estimate_market_impact(orderbook, quantity, volatility=0.01, T=1, gamma=2e-6, eta=2e-6):
    """
    Almgren-Chriss market impact model for estimating price impact of trades.
    
    This model captures both permanent and temporary price impacts of a trade.
    
    Parameters:
        orderbook (dict): Current orderbook snapshot
        quantity (float): Order size in USD
        volatility (float): Annualized volatility (fraction)
        T (float): Time horizon for execution (1 for immediate execution)
        gamma (float): Permanent impact parameter calibrated to market liquidity
                      (2e-6 is a reasonable value for liquid markets)
        eta (float): Temporary impact parameter calibrated to market liquidity
                    (2e-6 is a reasonable value for liquid markets)
    
    Returns:
        impact_cost (float): Expected market impact in USD
        
    Note:
        The parameters gamma and eta are calibrated based on market conditions
        and liquidity. Lower values indicate higher liquidity and lower impact.
        These should be periodically recalibrated based on market data.
    """
    try:
        # Extract top bid and ask prices from the orderbook
        top_bid = float(orderbook['bids'][0][0])
        top_ask = float(orderbook['asks'][0][0])
        
        # Calculate mid price
        mid_price = (top_bid + top_ask) / 2
        
        # Convert USD amount to asset units
        Q = float(quantity) / mid_price
        
        # Calculate permanent impact component
        # This is the lasting effect on market price after trade completion
        permanent = gamma * Q
        
        # Calculate temporary impact component
        # This is the immediate price concession needed to execute the trade
        temporary = eta * Q / T
        
        # Calculate total impact in USD
        impact_cost = (permanent + temporary) * mid_price
        
        return impact_cost
    except Exception as e:
        # Handle errors gracefully and return 0 as a safe default
        print(f"Error estimating market impact: {e}")
        return 0.0

# --- Maker/Taker Logistic Regression Model ---
# Training data structure: [order size, spread], is_maker (1=maker, 0=taker)
# This model predicts the probability that a trade will be executed as a maker order
# Features:
#   - order_size: The size of the order in USD
#   - spread: The bid-ask spread in the orderbook
# Target:
#   - is_maker: Binary classification (1 for maker, 0 for taker)

X_mt = np.array([
    [100, 0.5],  # Small order, narrow spread -> likely maker
    [200, 0.5],  # Larger order, narrow spread -> likely taker
    [100, 1.0],  # Small order, wide spread -> likely maker
    [200, 1.0]   # Larger order, wide spread -> likely taker
])
y_mt = np.array([1, 0, 1, 0])  # 1 = maker, 0 = taker

# Train the logistic regression model
# This model captures the relationship: P(maker) = 1 / (1 + e^-(β₀ + β₁*order_size + β₂*spread))
maker_taker_model = LogisticRegression().fit(X_mt, y_mt)

def predict_maker_taker(orderbook, quantity):
    """
    Predict maker/taker proportion using logistic regression.
    
    This function estimates the probability that an order will be executed as a
    maker order vs. a taker order, based on order size and current market spread.
    
    For market orders, this will typically return values close to 0, indicating
    taker execution. For limit orders placed away from the market, this would
    return higher values, indicating maker execution likelihood.
    
    Args:
        orderbook (dict): Current orderbook snapshot with bids and asks
        quantity (float): Order size in USD
    
    Returns:
        float: Probability of being executed as a maker order (0.0-1.0)
    """
    try:
        # Extract top bid and ask prices from the orderbook
        top_bid = float(orderbook['bids'][0][0])
        top_ask = float(orderbook['asks'][0][0])
        
        # Calculate the current spread
        spread = top_ask - top_bid
        
        # Prepare input features: [order_size, spread]
        X = np.array([[float(quantity), spread]])
        
        # Predict probability of maker execution using the pre-trained model
        # This returns the probability of class 1 (maker)
        prob = maker_taker_model.predict_proba(X)[0][1]
        
        return prob
    except Exception as e:
        # Handle errors gracefully and return 0.5 as a balanced default
        print(f"Error predicting maker/taker proportion: {e}")
        return 0.5 