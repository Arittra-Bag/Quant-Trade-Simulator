# Quant Trade Simulator Documentation

## Model Selection and Parameters

### Slippage Estimation Model
The application uses a linear regression model to estimate slippage based on order size and market volatility. Linear regression was chosen for its:
- Simplicity and interpretability
- Ability to capture the linear relationship between order size, volatility, and slippage
- Computational efficiency for real-time processing

The model is trained on historical data with features:
- Order size (USD value)
- Market volatility (measured as the annualized standard deviation of returns)

The output is the expected slippage in USD terms.

### Almgren-Chriss Market Impact Model
The Almgren-Chriss model is implemented for estimating market impact of large orders. This model was selected because:
- It provides a well-established mathematical framework for estimating both temporary and permanent price impacts
- It accounts for order size, execution time, and market characteristics
- It has strong theoretical foundations in financial economics

The model uses the following parameters:
- `gamma` (2e-6): Permanent impact parameter that represents how much the market price is expected to change permanently as a result of the trade
- `eta` (2e-6): Temporary impact parameter that represents the immediate price concession to execute the trade
- `T`: Time horizon for execution (set to 1 as default, representing immediate execution for market orders)

The formula implemented is:
```
Impact = (permanent_impact + temporary_impact) * mid_price
Where:
permanent_impact = gamma * Q
temporary_impact = eta * Q / T
Q = quantity in asset units
```

### Maker/Taker Proportion Model
A logistic regression model is used to predict the probability of an order being executed as a maker order vs. a taker order. This model:
- Maps the input variables to a probability between 0 and 1
- Accounts for order size and current bid-ask spread
- Provides a probabilistic framework for estimating maker vs. taker execution likelihood

For market orders, this will typically predict near-zero values, indicating taker executions.

### Fee Model
A rule-based fee model is implemented based on OKX's tier-based fee structure:
- Tier 1: 0.08% (0.0008)
- Tier 2: 0.07% (0.0007)
- Tier 3: 0.06% (0.0006)

Fees are calculated by multiplying the order quantity by the appropriate tier rate.

### Gemini AI Integration
The application integrates Google's Gemini AI to provide market analysis and trading strategy recommendations:
- Market sentiment analysis (Bullish, Bearish, or Neutral)
- Order imbalance interpretation
- Trading strategy recommendations based on orderbook data and transaction costs
- Execution approach suggestions

The Gemini integration:
- Uses the gemini-2.0-flash model
- Securely stores API credentials in environment variables
- Formats orderbook data into structured prompts
- Processes JSON responses for clean UI presentation

## Environment Configuration
The application uses environment variables for sensitive configuration:

1. **API Keys**
   - `GEMINI_API_KEY`: Required for Google Gemini AI integration
   - Stored in `.env` file (excluded from version control)
   - Loaded using python-dotenv

2. **Security Practices**
   - No hardcoded API keys in source code
   - .env files excluded from Git via .gitignore
   - Graceful handling when credentials are missing

To configure:
1. Create a `.env` file in the project root
2. Add `GEMINI_API_KEY=your_key_here`
3. The application will automatically load this configuration

## Regression Techniques

### Linear Regression for Slippage
The linear regression model for slippage uses ordinary least squares (OLS) to fit a linear equation:
```
Slippage = β₀ + β₁*OrderSize + β₂*Volatility
```
Where β₀, β₁, and β₂ are coefficients learned from historical data.

The model is trained using scikit-learn's LinearRegression implementation, which:
- Minimizes the residual sum of squares between observed and predicted values
- Handles multiple features efficiently
- Provides interpretable coefficients that indicate the impact of each feature on slippage

### Logistic Regression for Maker/Taker Proportion
The logistic regression model uses the logistic function to map any real-valued number to a value between 0 and 1:
```
P(Maker) = 1 / (1 + e^(-z))
where z = β₀ + β₁*OrderSize + β₂*Spread
```

This model:
- Outputs a probability between 0 and 1
- Uses order size and current spread as input features
- Is trained with scikit-learn's LogisticRegression implementation

## Market Impact Calculation Methodology

The Almgren-Chriss model breaks market impact into two components:

1. **Permanent Impact**: The lasting effect on market price after the trade is completed
   - Calculated as `gamma * Q` where Q is the order quantity in asset units
   - This impact remains in the market after the trade and affects all future trades

2. **Temporary Impact**: The immediate price concession needed to execute the trade
   - Calculated as `eta * Q / T` where T is the execution time
   - This impact is transient and dissipates after the trade is completed

The total market impact is the sum of these components, converted to USD:
```
impact_cost = (permanent_impact + temporary_impact) * mid_price
```

The parameters `gamma` and `eta` are calibrated based on historical market data and represent the market's liquidity characteristics.

## Performance Optimization Approaches

### Memory Management
1. **Data Structure Efficiency**:
   - Minimize data copying by using references and in-place operations
   - Store only necessary orderbook levels (top 50) for analysis
   - Use optimized numpy arrays for numerical computations

2. **File-Based Communication**:
   - The application uses a file-based approach for communication between the WebSocket client and UI
   - Temporary files are used during writing to avoid partial reads
   - File operations are optimized with a minimum update interval to prevent excessive I/O

### Network Communication
1. **Efficient WebSocket Implementation**:
   - Asynchronous WebSocket client using Python's asyncio library
   - Connection reestablishment logic with exponential backoff
   - Minimal data transformation to reduce processing overhead

2. **Update Frequency Control**:
   - Configurable update interval to balance between real-time updates and system load
   - Validation of incoming data before processing to filter out invalid messages

### Thread Management
1. **Process Separation**:
   - WebSocket client runs as a separate process to avoid UI blocking
   - Decoupled architecture allows WebSocket operation independent of UI refresh rate

2. **Async Processing**:
   - Asynchronous programming model for WebSocket client
   - Non-blocking I/O operations

### Regression Model Efficiency
1. **Model Simplicity**:
   - Linear and logistic regression models chosen for computational efficiency
   - Simple models with few parameters that can be evaluated quickly
   - Pre-trained models with no runtime training requirements

2. **Vectorized Operations**:
   - Use of numpy for efficient numerical operations
   - Batch prediction for all metrics in one pass to minimize overhead

## Latency Benchmarking

### Data Processing Latency
The application measures the time taken to process each orderbook update and calculate all metrics. This includes:
- Slippage estimation
- Fee calculation
- Market impact calculation
- Maker/Taker proportion prediction

The latency is reported in milliseconds in the UI, typically showing values between 1-5ms depending on system load.

### UI Update Latency
The interval component in the Dash application is set to 500ms, which provides a balance between:
- Responsive UI updates
- System resource utilization
- Human perception thresholds

### End-to-End Simulation Loop Latency
The complete latency from data reception to UI update includes:
1. WebSocket message reception
2. Data validation and processing
3. File writing
4. File reading by the UI
5. Metric calculation
6. UI rendering

This full loop typically completes in under 600ms, ensuring that the system can process data faster than it is received from the WebSocket feed.

## Future Improvements

1. **Real-time Model Training**:
   - Implement online learning for the regression models
   - Adapt model parameters based on recent market conditions

2. **Advanced Market Impact Models**:
   - Integrate more sophisticated market impact models
   - Account for market-specific factors like volatility regime

3. **Enhanced Performance Monitoring**:
   - Add detailed performance metrics and dashboards
   - Implement monitoring for system resource utilization

4. **Database Integration**:
   - Replace file-based communication with a lightweight database
   - Store historical data for model training and validation 