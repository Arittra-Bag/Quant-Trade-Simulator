import os
import google.generativeai as genai
import json
import time
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Get API key from environment variables
API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    print("Warning: GEMINI_API_KEY not found in environment variables. Some features may not work.")

# Configure the Gemini API with the key from environment variables
if API_KEY:
    genai.configure(api_key=API_KEY)

class GeminiAnalyzer:
    def __init__(self):
        """Initialize the Gemini Analyzer with the Generation model."""
        if API_KEY:
            self.model = genai.GenerativeModel('gemini-2.0-flash')
        else:
            self.model = None
        self.last_call_time = 0
        self.min_interval = 5  # Minimum seconds between API calls to avoid rate limits
        
    def analyze_orderbook(self, orderbook_data):
        """
        Analyze the current orderbook data and return insights.
        
        Args:
            orderbook_data: Dictionary containing orderbook data
        
        Returns:
            Dictionary with analysis results
        """
        # Check if API key is available
        if not API_KEY or not self.model:
            return {
                "success": False,
                "sentiment": "Neutral",
                "analysis": "Gemini API key not configured. Please set GEMINI_API_KEY environment variable."
            }
            
        # Handle empty or invalid orderbook data
        if not orderbook_data or not isinstance(orderbook_data, dict):
            return {
                "success": False,
                "sentiment": "Neutral",
                "analysis": "No valid orderbook data available for analysis."
            }
            
        # Extract orderbook data
        try:
            bids = orderbook_data.get("bids", [])
            asks = orderbook_data.get("asks", [])
            
            if not bids or not asks:
                return {
                    "success": False,
                    "sentiment": "Neutral",
                    "analysis": "Orderbook data is missing bid or ask information."
                }
                
            # Continue with analysis
        except Exception as e:
            return {
                "success": False,
                "sentiment": "Neutral",
                "analysis": f"Error analyzing orderbook data: {str(e)}"
            }
        
        try:
            # Calculate basic metrics for prompt
            bids = orderbook_data['bids'][:5]  # Top 5 bids
            asks = orderbook_data['asks'][:5]  # Top 5 asks
            
            # Calculate bid-ask spread
            top_bid = float(bids[0][0]) if bids else 0
            top_ask = float(asks[0][0]) if asks else 0
            spread = top_ask - top_bid if top_bid and top_ask else 0
            
            # Calculate total volume
            bid_volume = sum(float(bid[1]) for bid in bids)
            ask_volume = sum(float(ask[1]) for ask in asks)
            
            # Calculate imbalance
            imbalance = (bid_volume - ask_volume) / (bid_volume + ask_volume) if (bid_volume + ask_volume) > 0 else 0
            
            # Create prompt for Gemini
            prompt = f"""
            Analyze this cryptocurrency orderbook snapshot and provide insights:
            
            Asset: {orderbook_data.get('symbol', 'BTC-USDT-SWAP')}
            Time: {orderbook_data.get('timestamp', 'Unknown')}
            
            Top 5 Bids (Buy Orders):
            {json.dumps(bids, indent=2)}
            
            Top 5 Asks (Sell Orders):
            {json.dumps(asks, indent=2)}
            
            Key Metrics:
            - Bid-Ask Spread: {spread:.2f}
            - Bid Volume: {bid_volume:.2f}
            - Ask Volume: {ask_volume:.2f}
            - Order Imbalance: {imbalance:.2f} (positive means more buying pressure)
            
            Please provide:
            1. Market sentiment (Bullish, Bearish, or Neutral)
            2. Brief analysis (2-3 sentences)
            3. A trading recommendation
            
            Format your response as a JSON object with fields: sentiment, analysis, recommendation.
            Be concise and focus only on the data provided.
            """
            
            # Call Gemini API
            response = self.model.generate_content(prompt)
            self.last_call_time = time.time()
            
            # Process response
            result_text = response.text
            
            # Try to extract JSON
            try:
                # First try to find and parse JSON if it's within a code block
                if "```json" in result_text and "```" in result_text.split("```json")[1]:
                    json_str = result_text.split("```json")[1].split("```")[0].strip()
                    result = json.loads(json_str)
                elif "{" in result_text and "}" in result_text:
                    # If not in code block, try to extract the JSON object
                    json_str = result_text[result_text.find("{"):result_text.rfind("}")+1]
                    result = json.loads(json_str)
                else:
                    # If JSON extraction fails, create structured response from text
                    result = {
                        "sentiment": "Neutral",
                        "analysis": result_text[:200] + "..." if len(result_text) > 200 else result_text,
                        "recommendation": "See analysis above."
                    }
            except json.JSONDecodeError:
                # If JSON parsing fails, create a structured response
                result = {
                    "sentiment": "Neutral",
                    "analysis": result_text[:200] + "..." if len(result_text) > 200 else result_text,
                    "recommendation": "See analysis above."
                }
            
            result["success"] = True
            return result
            
        except Exception as e:
            # Handle any exceptions
            return {
                "sentiment": "Error",
                "analysis": f"Error during analysis: {str(e)}",
                "recommendation": "Try again later.",
                "success": False
            }
    
    def get_trading_strategy(self, orderbook_data, quantity, fees, slippage, impact):
        """
        Generate a specific trading strategy recommendation based on market data and costs.
        
        Args:
            orderbook_data (dict): L2 orderbook data with bids and asks
            quantity (float): Order quantity in USD
            fees (float): Expected fees
            slippage (float): Expected slippage
            impact (float): Expected market impact
            
        Returns:
            dict: Strategy recommendation
        """
        try:
            # Check if API key is available
            if not API_KEY or not self.model:
                return {
                    "strategy": "API Not Configured",
                    "reasoning": "Gemini API key not configured. Please set GEMINI_API_KEY environment variable.",
                    "execution_approach": "Configure API key first",
                    "success": False
                }
                
            # Extract relevant data
            if not orderbook_data:
                return {
                    "strategy": "Insufficient data for strategy recommendation.",
                    "reasoning": "No orderbook data available.",
                    "success": False
                }
            
            # Calculate basic metrics for prompt
            symbol = orderbook_data.get('symbol', 'BTC-USDT-SWAP')
            top_bid = float(orderbook_data['bids'][0][0]) if orderbook_data.get('bids') else 0
            top_ask = float(orderbook_data['asks'][0][0]) if orderbook_data.get('asks') else 0
            mid_price = (top_bid + top_ask) / 2 if top_bid and top_ask else 0
            
            # Total transaction cost
            total_cost = fees + slippage + impact
            cost_percentage = (total_cost / quantity) * 100 if quantity > 0 else 0
            
            # Create prompt for Gemini
            prompt = f"""
            Generate an optimal trading strategy based on this data:
            
            Asset: {symbol}
            Order Size: ${quantity:.2f}
            Current Mid Price: ${mid_price:.2f}
            
            Transaction Costs:
            - Fees: ${fees:.4f} ({(fees/quantity)*100:.4f}% of order)
            - Expected Slippage: ${slippage:.4f} ({(slippage/quantity)*100:.4f}% of order)
            - Market Impact: ${impact:.4f} ({(impact/quantity)*100:.4f}% of order)
            - Total Cost: ${total_cost:.4f} ({cost_percentage:.4f}% of order)
            
            Based only on this data, recommend a specific trading strategy.
            
            Format your response as JSON with these fields:
            - strategy: Brief strategy name/type
            - reasoning: 1-2 sentences explaining your recommendation
            - execution_approach: Brief execution approach
            
            Be extremely concise.
            """
            
            # Call Gemini API
            response = self.model.generate_content(prompt)
            self.last_call_time = time.time()
            
            # Process response
            result_text = response.text
            
            # Try to extract JSON
            try:
                # First try to find and parse JSON if it's within a code block
                if "```json" in result_text and "```" in result_text.split("```json")[1]:
                    json_str = result_text.split("```json")[1].split("```")[0].strip()
                    result = json.loads(json_str)
                elif "{" in result_text and "}" in result_text:
                    # If not in code block, try to extract the JSON object
                    json_str = result_text[result_text.find("{"):result_text.rfind("}")+1]
                    result = json.loads(json_str)
                else:
                    # If JSON extraction fails, create structured response from text
                    result = {
                        "strategy": "Direct Market Execution",
                        "reasoning": result_text[:200] + "..." if len(result_text) > 200 else result_text,
                        "execution_approach": "Standard market order"
                    }
            except json.JSONDecodeError:
                # If JSON parsing fails, create a structured response
                result = {
                    "strategy": "Direct Market Execution",
                    "reasoning": result_text[:200] + "..." if len(result_text) > 200 else result_text,
                    "execution_approach": "Standard market order"
                }
            
            result["success"] = True
            return result
            
        except Exception as e:
            # Handle any exceptions
            return {
                "strategy": "Error generating strategy",
                "reasoning": f"Error: {str(e)}",
                "execution_approach": "Try again later",
                "success": False
            } 