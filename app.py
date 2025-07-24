import dash
from dash import dcc, html, Input, Output, State, ctx, dash_table
import json
import time
import os
import subprocess
import signal
import plotly.graph_objs as go
from datetime import datetime
import dash_bootstrap_components as dbc
import base64
import threading
import queue
from models import estimate_slippage, estimate_market_impact, predict_maker_taker
from fee_model import calculate_fees
from utils import measure_latency
from visualizations import create_orderbook_depth_chart, create_latency_time_series, create_transaction_cost_breakdown
from gemini_integration import GeminiAnalyzer
from export import export_orderbook_to_csv, export_orderbook_to_excel

# File to store the latest orderbook data
ORDERBOOK_FILE = "latest_orderbook.json"

# Initialize global variables
data_last_modified = 0
orderbook_data = None
client_process = None
is_streaming = False
update_count = 0
latency_measurements = []
metrics_history = []

# Thread-safe queue for chart data
chart_data_queue = queue.Queue(maxsize=1)
# Flag to control background threads
keep_threads_running = True

# Initialize Gemini Analyzer
gemini_analyzer = GeminiAnalyzer()

# Add a global variable to track if streaming is active
is_updating_charts = False

# Initialize Dash app
app = dash.Dash(
    __name__, 
    external_stylesheets=[dbc.themes.DARKLY],
    update_title=None,  # Remove "Updating..." from title on updates
    meta_tags=[
        {"name": "viewport", "content": "width=device-width, initial-scale=1"}
    ]
)

app.title = "Quant Trade Simulator"

# Add custom CSS for better visibility
app.index_string = '''
<!DOCTYPE html>
<html>
    <head>
        {%metas%}
        <title>{%title%}</title>
        {%favicon%}
        {%css%}
        <style>
            .nav-tabs .nav-item .nav-link {
                color: white !important;
                background-color: #444;
                margin-right: 5px;
                border-radius: 4px 4px 0 0;
            }
            .nav-tabs .nav-item .nav-link.active {
                background-color: #222;
                border-color: #444;
                font-weight: bold;
            }
            .status-text.active {
                color: #4ECB71;
                font-weight: bold;
            }
            .status-text.inactive {
                color: #FF6B6B;
                font-weight: bold;
            }
            .card {
                border: 1px solid #444;
                margin-bottom: 15px;
            }
            .debug-text {
                max-height: 200px;
                overflow-y: auto;
            }
            .metric-value {
                font-size: 1.5rem;
            }
        </style>
    </head>
    <body>
        {%app_entry%}
        <footer>
            {%config%}
            {%scripts%}
            {%renderer%}
        </footer>
    </body>
</html>
'''

# Function to start the WebSocket client in a subprocess
def start_websocket_client(symbol):
    cmd = ["python", "websocket_client.py", "--symbol", symbol]
    process = subprocess.Popen(cmd)
    return process

# Function to stop the WebSocket client
def stop_websocket_client(process):
    if process is not None:
        try:
            if os.name == 'nt':  # Windows
                # Force kill to ensure it stops completely
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)], 
                               stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            else:  # Unix/Linux/Mac
                process.send_signal(signal.SIGTERM)
            # Wait for process to terminate
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                # If still running after timeout, force kill
                process.kill()
                process.wait(timeout=2)
        except Exception as e:
            print(f"Error stopping websocket client: {e}")
            # Ensure process is killed as last resort
            try:
                process.kill()
            except:
                pass

# Function to read orderbook data from file
def read_orderbook_data(last_modified):
    try:
        if os.path.exists(ORDERBOOK_FILE):
            modified_time = os.path.getmtime(ORDERBOOK_FILE)
            # Only read if the file has been modified since last read
            if modified_time > last_modified:
                with open(ORDERBOOK_FILE, 'r') as f:
                    data = json.load(f)
                return data, modified_time
    except Exception as e:
        print(f"Error reading orderbook data: {e}")
    return None, last_modified

# Layout: Two-column design with inputs on left, outputs on right
app.layout = dbc.Container([
    dbc.Row([
        dbc.Col([
            html.H1("Quant Trade Simulator", className="mb-4 mt-3")
        ], width=12)
    ]),
    
    dbc.Row([
        # Input parameters (left side)
        dbc.Col([
            html.H3("Input Parameters", className="mb-3"),
            dbc.Card([
                dbc.CardBody([
                    html.Div([
                        dbc.Label("Exchange"),
                        dcc.Dropdown(
                            id="exchange-dropdown",
                            options=[{"label": "OKX", "value": "OKX"}],
                            value="OKX",
                            clearable=False
                        ),
                    ], className="mb-3"),
                    
                    html.Div([
                        dbc.Label("Spot Asset"),
                        dbc.Input(id="asset-input", type="text", value="BTC-USDT-SWAP"),
                    ], className="mb-3"),
                    
                    html.Div([
                        dbc.Label("Order Type"),
                        dcc.Dropdown(
                            id="order-type-dropdown",
                            options=[{"label": "Market", "value": "Market"}],
                            value="Market",
                            clearable=False
                        ),
                    ], className="mb-3"),
                    
                    html.Div([
                        dbc.Label("Quantity (USD)"),
                        dcc.Slider(
                            id="quantity-slider",
                            min=1,
                            max=1000,
                            value=100,
                            marks={i: str(i) for i in [1, 100, 500, 1000]},
                            tooltip={"placement": "bottom", "always_visible": True}
                        ),
                    ], className="mb-3"),
                    
                    html.Div([
                        dbc.Label("Volatility"),
                        dcc.Slider(
                            id="volatility-slider",
                            min=0.001,
                            max=0.05,
                            step=0.001,
                            value=0.01,
                            marks={i: f"{i:.3f}" for i in [0.001, 0.01, 0.02, 0.05]},
                            tooltip={"placement": "bottom", "always_visible": True}
                        ),
                    ], className="mb-3"),
                    
                    html.Div([
                        dbc.Label("Fee Tier"),
                        dcc.Dropdown(
                            id="fee-tier-dropdown",
                            options=[
                                {"label": "Tier 1", "value": "Tier 1"},
                                {"label": "Tier 2", "value": "Tier 2"},
                                {"label": "Tier 3", "value": "Tier 3"}
                            ],
                            value="Tier 1",
                            clearable=False
                        ),
                    ], className="mb-3"),
                    
                    html.Div([
                        dbc.Button("Start Stream", id="start-button", color="success", className="mr-2"),
                        dbc.Button("Stop Stream", id="stop-button", color="danger")
                    ], className="d-flex justify-content-between mt-3")
                ])
            ]),
            
            # Status card
            dbc.Card([
                dbc.CardBody([
                    html.H5("Stream Status", className="card-title"),
                    html.Div(id="status-display", className="status-text"),
                    html.Div(id="update-time", className="text-muted mt-2 small")
                ])
            ], className="mt-3"),

            # Export options card
            dbc.Card([
                dbc.CardHeader([
                    html.H5("Export Options", className="card-title mb-0"),
                ], style={"background-color": "#375a7f"}),
                dbc.CardBody([
                    html.Div([
                        dbc.Button("Export CSV", id="export-csv-button", color="info", className="me-2 mb-2", size="lg"),
                        dbc.Button("Export Excel", id="export-excel-button", color="success", className="me-2 mb-2", size="lg"),
                    ]),
                    dcc.Download(id="download-data")
                ])
            ], className="mt-3"),

            # Gemini AI Analysis
            dbc.Card([
                dbc.CardHeader([
                    html.H5("Gemini AI Market Analysis", className="card-title mb-0")
                ], style={"background-color": "#375a7f"}),
                dbc.CardBody([
                    html.Div(id="gemini-analysis", style={"min-height": "100px"}),
                    dbc.Button("Generate Analysis", id="generate-analysis-button", color="primary", className="mt-3", size="lg")
                ])
            ], className="mt-4")
        ], md=4),
        
        # Output parameters and orderbook display (right side)
        dbc.Col([
            html.H3("Output Parameters", className="mb-3"),
            
            # Output metrics
            dbc.Row([
                dbc.Col([
                    dbc.Card([
                        dbc.CardBody([
                            html.H5("Expected Slippage"),
                            html.H3(id="slippage-value", className="metric-value")
                        ])
                    ])
                ], md=4),
                dbc.Col([
                    dbc.Card([
                        dbc.CardBody([
                            html.H5("Expected Fees"),
                            html.H3(id="fees-value", className="metric-value")
                        ])
                    ])
                ], md=4),
                dbc.Col([
                    dbc.Card([
                        dbc.CardBody([
                            html.H5("Market Impact"),
                            html.H3(id="impact-value", className="metric-value")
                        ])
                    ])
                ], md=4),
            ], className="mb-3"),
            
            dbc.Row([
                dbc.Col([
                    dbc.Card([
                        dbc.CardBody([
                            html.H5("Net Cost"),
                            html.H3(id="netcost-value", className="metric-value")
                        ])
                    ])
                ], md=4),
                dbc.Col([
                    dbc.Card([
                        dbc.CardBody([
                            html.H5("Maker/Taker"),
                            html.H3(id="makertaker-value", className="metric-value")
                        ])
                    ])
                ], md=4),
                dbc.Col([
                    dbc.Card([
                        dbc.CardBody([
                            html.H5("Latency"),
                            html.H3(id="latency-value", className="metric-value")
                        ])
                    ])
                ], md=4),
            ], className="mb-3"),
            
            # Visualizations
            dbc.Tabs([
                dbc.Tab([
                    dbc.Row([
                        dbc.Col([
                            html.H5("Asks (Sell Orders)", className="mt-3"),
                            html.Div(id="asks-table")
                        ], md=6),
                        dbc.Col([
                            html.H5("Bids (Buy Orders)", className="mt-3"),
                            html.Div(id="bids-table")
                        ], md=6),
                    ]),
                ], label="Orderbook Tables", tab_id="tab-orderbook", label_style={"color": "white"}),
                
                dbc.Tab([
                    dcc.Graph(id="depth-chart")
                ], label="Depth Chart", tab_id="tab-depth", label_style={"color": "white"}),
                
                dbc.Tab([
                    dbc.Row([
                        dbc.Col([
                            dcc.Graph(id="latency-chart")
                        ], md=6),
                        dbc.Col([
                            dcc.Graph(id="cost-breakdown-chart")
                        ], md=6),
                    ]),
                ], label="Performance", tab_id="tab-performance", label_style={"color": "white"}),
            ], id="visualization-tabs", active_tab="tab-orderbook", className="mt-4"),
            
            # Debug information
            dbc.Card([
                dbc.CardHeader("Debug Information"),
                dbc.CardBody([
                    html.Pre(id="debug-info", className="debug-text")
                ])
            ], className="mt-3")
        ], md=8),
    ]),
    
    # Interval component for polling
    dcc.Interval(
        id="interval-component",
        interval=500,  # in milliseconds
        n_intervals=0
    ),

    # Slower interval for chart updates
    dcc.Interval(
        id="chart-interval-component",
        interval=3000,  # 3 seconds
        n_intervals=0,
        disabled=True
    ),
], fluid=True)

@app.callback(
    Output("status-display", "children"),
    Output("status-display", "className"),
    Output("chart-interval-component", "disabled"),
    Input("start-button", "n_clicks"),
    Input("stop-button", "n_clicks"),
    State("asset-input", "value"),
    prevent_initial_call=True
)
def handle_stream_control(start_clicks, stop_clicks, asset):
    global client_process, is_streaming
    
    triggered_id = ctx.triggered_id
    
    if triggered_id == "start-button" and start_clicks:
        if not is_streaming:
            client_process = start_websocket_client(asset)
            is_streaming = True
            return "Stream active", "status-text active", False  # Enable chart updates
        return "Stream already active", "status-text active", False
    
    elif triggered_id == "stop-button" and stop_clicks:
        if is_streaming:
            stop_websocket_client(client_process)
            client_process = None
            is_streaming = False
            return "Stream stopped", "status-text inactive", True  # Disable chart updates
        return "No active stream to stop", "status-text inactive", True
    
    return "Unknown status", "status-text", True

@app.callback(
    [Output("update-time", "children"),
     Output("slippage-value", "children"),
     Output("fees-value", "children"),
     Output("impact-value", "children"),
     Output("netcost-value", "children"),
     Output("makertaker-value", "children"),
     Output("latency-value", "children"),
     Output("asks-table", "children"),
     Output("bids-table", "children"),
     Output("debug-info", "children")],
    Input("interval-component", "n_intervals"),
    [State("quantity-slider", "value"),
     State("volatility-slider", "value"),
     State("fee-tier-dropdown", "value")]
)
def update_tables(n_intervals, quantity, volatility, fee_tier):
    global orderbook_data, data_last_modified, update_count, latency_measurements, metrics_history
    
    if not is_streaming:
        return (
            f"Last Update: {datetime.now().strftime('%H:%M:%S')} - Update #{update_count}",
            dash.no_update, dash.no_update, dash.no_update, 
            dash.no_update, dash.no_update, dash.no_update,
            dash.no_update, dash.no_update, dash.no_update
        )
    
    # Start timing for latency measurement
    start_time = time.time()
    
    # Default values
    slippage_value = "$0.00"
    fees_value = "$0.00"
    impact_value = "$0.00"
    netcost_value = "$0.00"
    makertaker_value = "0.00%"
    latency_value = "0 ms"
    debug_info = "No data available"
    asks_table = "No data"
    bids_table = "No data"
    
    # Read orderbook data from file
    new_data, new_last_modified = read_orderbook_data(data_last_modified)
    
    if new_data:
        orderbook_data = new_data
        data_last_modified = new_last_modified
        update_count += 1
        
        # Calculate metrics
        slippage = estimate_slippage(orderbook_data, quantity, volatility)
        fees = calculate_fees(quantity, fee_tier)
        impact = estimate_market_impact(orderbook_data, quantity, volatility)
        maker_taker_prob = predict_maker_taker(orderbook_data, quantity)
        
        # Calculate net cost
        net_cost = slippage + fees + impact
        
        # Store metrics for history
        metrics_entry = {
            "timestamp": time.time(),
            "slippage": slippage,
            "fees": fees,
            "impact": impact,
            "net_cost": net_cost,
            "maker_taker": maker_taker_prob,
            "quantity": quantity,
            "volatility": volatility
        }
        metrics_history.append(metrics_entry)
        if len(metrics_history) > 100:
            metrics_history.pop(0)
        
        # Format output values
        slippage_value = f"${slippage:.4f}"
        fees_value = f"${fees:.4f}"
        impact_value = f"${impact:.4f}"
        netcost_value = f"${net_cost:.4f}"
        makertaker_value = f"{maker_taker_prob:.2%}"
        
        # Format orderbook tables
        asks = orderbook_data.get('asks', [])[:5]
        bids = orderbook_data.get('bids', [])[:5]
        
        asks_table = dash_table.DataTable(
            columns=[{"name": "Price", "id": "price"}, {"name": "Size", "id": "size"}],
            data=[{"price": ask[0], "size": ask[1]} for ask in asks],
            style_cell={'textAlign': 'center', 'backgroundColor': '#303030', 'color': 'white'},
            style_header={'backgroundColor': '#404040', 'fontWeight': 'bold'},
            style_data_conditional=[
                {
                    'if': {'column_id': 'price'},
                    'color': '#FF6B6B'
                }
            ]
        )
        
        bids_table = dash_table.DataTable(
            columns=[{"name": "Price", "id": "price"}, {"name": "Size", "id": "size"}],
            data=[{"price": bid[0], "size": bid[1]} for bid in bids],
            style_cell={'textAlign': 'center', 'backgroundColor': '#303030', 'color': 'white'},
            style_header={'backgroundColor': '#404040', 'fontWeight': 'bold'},
            style_data_conditional=[
                {
                    'if': {'column_id': 'price'},
                    'color': '#4ECB71'
                }
            ]
        )
        
        # Prepare debug information
        debug_info = json.dumps(orderbook_data, indent=2)
    
    # Calculate latency
    latency = measure_latency(start_time)
    latency_value = f"{latency:.2f} ms"
    
    # Update latency measurements for chart
    latency_measurements.append(latency)
    if len(latency_measurements) > 50:
        latency_measurements.pop(0)
    
    update_time = f"Last Update: {datetime.now().strftime('%H:%M:%S')} - Update #{update_count}"
    
    return (
        update_time,
        slippage_value,
        fees_value,
        impact_value,
        netcost_value,
        makertaker_value,
        latency_value,
        asks_table,
        bids_table,
        debug_info
    )

# Background chart generation thread
def chart_generation_thread():
    global orderbook_data, keep_threads_running
    
    while keep_threads_running:
        # Only generate new charts if we have data and streaming is active
        if is_streaming and orderbook_data is not None:
            try:
                # Default values if no specific request is in the queue
                quantity = 100
                volatility = 0.01
                fee_tier = "Tier 1"
                
                # Calculate metrics for charts
                slippage = estimate_slippage(orderbook_data, quantity, volatility)
                fees = calculate_fees(quantity, fee_tier)
                impact = estimate_market_impact(orderbook_data, quantity, volatility)
                
                # Create charts
                depth_chart = create_orderbook_depth_chart(orderbook_data)
                latency_chart = create_latency_time_series(latency_measurements)
                cost_breakdown_chart = create_transaction_cost_breakdown(slippage, fees, impact)
                
                # Put charts in queue, replacing any old data
                # Use put_nowait and handle queue full exceptions to avoid blocking
                try:
                    chart_data = {
                        "depth_chart": depth_chart,
                        "latency_chart": latency_chart,
                        "cost_breakdown_chart": cost_breakdown_chart,
                        "timestamp": time.time()
                    }
                    
                    # Try to put in queue, if full, remove old data first
                    if chart_data_queue.full():
                        try:
                            chart_data_queue.get_nowait()
                        except queue.Empty:
                            pass
                    
                    chart_data_queue.put_nowait(chart_data)
                except queue.Full:
                    # Queue is full, just continue
                    pass
            except Exception as e:
                print(f"Error in chart generation thread: {e}")
        
        # Sleep to avoid excessive CPU usage
        time.sleep(3)  # Generate charts every 3 seconds

# Callback for Gemini analysis
@app.callback(
    Output("gemini-analysis", "children"),
    Input("generate-analysis-button", "n_clicks"),
    [State("quantity-slider", "value"),
     State("volatility-slider", "value"),
     State("fee-tier-dropdown", "value")],
    prevent_initial_call=True
)
def generate_gemini_analysis(n_clicks, quantity, volatility, fee_tier):
    if not orderbook_data:
        return html.Div("No orderbook data available for analysis", className="text-warning")

    # Calculate current metrics
    slippage = estimate_slippage(orderbook_data, quantity, volatility)
    fees = calculate_fees(quantity, fee_tier)
    impact = estimate_market_impact(orderbook_data, quantity, volatility)
    
    # Get orderbook analysis
    analysis = gemini_analyzer.analyze_orderbook(orderbook_data)
    
    if not analysis.get("success", False):
        return html.Div(f"Analysis error: {analysis.get('analysis', 'Unknown error')}", className="text-danger")
    
    # Get trading strategy
    strategy = gemini_analyzer.get_trading_strategy(orderbook_data, quantity, fees, slippage, impact)
    
    # Format analysis output
    sentiment_color = "success" if analysis.get("sentiment") == "Bullish" else (
        "danger" if analysis.get("sentiment") == "Bearish" else "warning"
    )
    
    return html.Div([
        dbc.Row([
            dbc.Col([
                html.H5("Market Sentiment"),
                html.Div(analysis.get("sentiment", "Neutral"), className=f"text-{sentiment_color} fw-bold"),
                html.Hr(),
                html.H5("Analysis"),
                html.P(analysis.get("analysis", "No analysis available")),
            ], md=6),
            dbc.Col([
                html.H5("Strategy"),
                html.Div(strategy.get("strategy", "No strategy available"), className="fw-bold"),
                html.Hr(),
                html.H5("Reasoning"),
                html.P(strategy.get("reasoning", "No reasoning available")),
                html.H5("Execution Approach"),
                html.P(strategy.get("execution_approach", "No approach available")),
            ], md=6),
        ])
    ])

# Callback for CSV and Excel export
@app.callback(
    Output("download-data", "data"),
    [Input("export-csv-button", "n_clicks"),
     Input("export-excel-button", "n_clicks")],
    prevent_initial_call=True
)
def export_data(csv_clicks, excel_clicks):
    if not orderbook_data:
        return dash.no_update
    
    # Determine which button was clicked
    trigger = ctx.triggered_id
    
    if trigger == "export-csv-button":
        csv_content = export_orderbook_to_csv(orderbook_data)
        if csv_content:
            return dcc.send_string(
                base64.b64decode(csv_content).decode('utf-8'),
                filename=f"orderbook_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            )
    
    elif trigger == "export-excel-button":
        excel_content = export_orderbook_to_excel(orderbook_data)
        if excel_content:
            return dcc.send_bytes(
                base64.b64decode(excel_content),
                filename=f"orderbook_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
            )
    
    return dash.no_update

# Updated callback for chart updates using thread-safe queue
@app.callback(
    [Output("depth-chart", "figure"),
     Output("latency-chart", "figure"),
     Output("cost-breakdown-chart", "figure")],
    Input("chart-interval-component", "n_intervals")
)
def update_chart_displays(n_intervals):
    # If not streaming, return empty charts
    if not is_streaming:
        return go.Figure(), go.Figure(), go.Figure()
    
    # Try to get latest chart data from queue
    try:
        # Non-blocking get with a short timeout
        chart_data = chart_data_queue.get(timeout=0.1)
        
        # Return the charts from the queue
        return (
            chart_data["depth_chart"],
            chart_data["latency_chart"],
            chart_data["cost_breakdown_chart"]
        )
    except queue.Empty:
        # Queue is empty, return empty charts or previous ones
        return go.Figure(), go.Figure(), go.Figure()
    except Exception as e:
        print(f"Error in chart display update: {e}")
        return go.Figure(), go.Figure(), go.Figure()

if __name__ == "__main__":
    # Start chart generation background thread
    chart_thread = threading.Thread(target=chart_generation_thread, daemon=True)
    chart_thread.start()
    
    try:
        # Get port from environment variable (Render requirement)
        port = int(os.environ.get("PORT", 8050))
        app.run(debug=False, use_reloader=False, host="0.0.0.0", port=port)
    finally:
        # Ensure background thread is terminated when app stops
        keep_threads_running = False
        if is_streaming and client_process:
            stop_websocket_client(client_process)
        
        chart_thread.join(timeout=2)  # Wait up to 2 seconds for thread to terminate 