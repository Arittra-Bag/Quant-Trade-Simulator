import plotly.graph_objs as go
import pandas as pd
import numpy as np

def create_orderbook_depth_chart(orderbook_data):
    """
    Create a depth chart visualization of the orderbook.
    
    Args:
        orderbook_data (dict): Orderbook data with bids and asks
        
    Returns:
        plotly.graph_objs.Figure: Depth chart figure
    """
    if not orderbook_data or 'bids' not in orderbook_data or 'asks' not in orderbook_data:
        # Return empty figure if no data
        return go.Figure()
    
    # Extract bid and ask data
    bids = orderbook_data['bids']
    asks = orderbook_data['asks']
    
    # Convert to dataframes
    bids_df = pd.DataFrame(bids, columns=['price', 'size'])
    asks_df = pd.DataFrame(asks, columns=['price', 'size'])
    
    # Convert to numeric
    bids_df['price'] = pd.to_numeric(bids_df['price'])
    bids_df['size'] = pd.to_numeric(bids_df['size'])
    asks_df['price'] = pd.to_numeric(asks_df['price'])
    asks_df['size'] = pd.to_numeric(asks_df['size'])
    
    # Sort
    bids_df = bids_df.sort_values('price', ascending=False)
    asks_df = asks_df.sort_values('price')
    
    # Calculate cumulative volume
    bids_df['cumulative'] = bids_df['size'].cumsum()
    asks_df['cumulative'] = asks_df['size'].cumsum()
    
    # Create figure
    fig = go.Figure()
    
    # Add bid depth
    fig.add_trace(go.Scatter(
        x=bids_df['price'],
        y=bids_df['cumulative'],
        fill='tozeroy',
        name='Bids',
        line=dict(color='rgba(0, 255, 0, 0.7)'),
        fillcolor='rgba(0, 255, 0, 0.3)'
    ))
    
    # Add ask depth
    fig.add_trace(go.Scatter(
        x=asks_df['price'],
        y=asks_df['cumulative'],
        fill='tozeroy',
        name='Asks',
        line=dict(color='rgba(255, 0, 0, 0.7)'),
        fillcolor='rgba(255, 0, 0, 0.3)'
    ))
    
    # Update layout
    fig.update_layout(
        title='Orderbook Depth Chart',
        xaxis_title='Price',
        yaxis_title='Cumulative Size',
        template='plotly_dark',
        legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='right', x=1),
        height=400,
        margin=dict(l=40, r=40, t=60, b=40)
    )
    
    return fig

def create_latency_time_series(latency_history):
    """
    Create a time series chart of latency measurements.
    
    Args:
        latency_history (list): List of latency measurements
        
    Returns:
        plotly.graph_objs.Figure: Latency time series figure
    """
    if not latency_history:
        # Return empty figure if no data
        return go.Figure()
    
    fig = go.Figure()
    
    # Add latency trace
    fig.add_trace(go.Scatter(
        y=latency_history,
        mode='lines',
        name='Latency',
        line=dict(color='cyan', width=2)
    ))
    
    # Add average line
    avg_latency = sum(latency_history) / len(latency_history)
    fig.add_trace(go.Scatter(
        y=[avg_latency] * len(latency_history),
        mode='lines',
        name=f'Average ({avg_latency:.2f} ms)',
        line=dict(color='yellow', width=1, dash='dash')
    ))
    
    # Update layout
    fig.update_layout(
        title='Processing Latency',
        xaxis_title='Measurements',
        yaxis_title='Latency (ms)',
        template='plotly_dark',
        height=300,
        margin=dict(l=40, r=40, t=60, b=40)
    )
    
    return fig

def create_transaction_cost_breakdown(slippage, fees, impact):
    """
    Create a pie chart showing the breakdown of transaction costs.
    
    Args:
        slippage (float): Expected slippage cost
        fees (float): Expected fees
        impact (float): Expected market impact
        
    Returns:
        plotly.graph_objs.Figure: Pie chart figure
    """
    labels = ['Fees', 'Slippage', 'Market Impact']
    values = [fees, slippage, impact]
    
    fig = go.Figure(data=[go.Pie(
        labels=labels,
        values=values,
        hole=.4,
        marker_colors=['#46BFBD', '#FF9500', '#F7464A']
    )])
    
    fig.update_layout(
        title='Transaction Cost Breakdown',
        template='plotly_dark',
        height=300,
        margin=dict(l=30, r=30, t=60, b=30),
        legend=dict(orientation='h', yanchor='bottom', y=-0.1, xanchor='center', x=0.5)
    )
    
    return fig 