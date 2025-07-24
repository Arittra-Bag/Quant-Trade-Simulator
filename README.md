# Quant Trade Simulator

A high-performance trade simulator leveraging real-time L2 orderbook data from OKX via WebSocket, with transaction cost and market impact estimation. Built with Python and Dash.

## Features
- Real-time L2 orderbook data processing with multi-endpoint fallback
- Slippage, fee, and market impact estimation
- Professional Dash/Plotly UI with Bootstrap styling
- Performance and latency metrics
- AI-powered market analysis using Google's Gemini
- Comprehensive documentation and performance analysis

## Setup
1. Clone the repository
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Set up environment variables:
   - Create a `.env` file in the project root
   - Add `GEMINI_API_KEY=your_key_here` for AI market analysis
   - Or set this environment variable in your system
   
4. Run the app:
   ```bash
   python app.py
   ```

## Main Components
- `app.py`: Dash application entry point
- `websocket_client.py`: WebSocket client for L2 orderbook
- `models.py`: Slippage, market impact, and regression models
- `fee_model.py`: Fee calculation logic
- `utils.py`: Helper functions (latency, logging, etc.)
- `gemini_integration.py`: AI-powered market analysis

## Documentation
The project includes comprehensive documentation:

- `DOCUMENTATION.md`: Detailed explanation of models, algorithms, and implementation
- `PERFORMANCE_ANALYSIS.md`: Performance benchmarks and optimization techniques

## Models Implemented
1. **Linear Regression for Slippage Estimation**
   - Predicts expected slippage based on order size and market volatility

2. **Almgren-Chriss Model for Market Impact**
   - Estimates both permanent and temporary price impacts
   - Accounts for order size, execution time, and market liquidity

3. **Logistic Regression for Maker/Taker Proportion**
   - Predicts probability of order executing as maker vs. taker
   - Uses order size and current market spread as features

4. **Rule-based Fee Model**
   - Calculates expected fees based on exchange fee tiers

5. **AI Market Analysis**
   - Uses Google's Gemini AI to analyze orderbook data
   - Provides market sentiment and trading strategy recommendations

## Future Enhancements
- Implement real-time model training based on market data
- Add more sophisticated market impact models
- Integrate with exchange APIs for actual order placement
- Develop advanced trade strategy backtesting 