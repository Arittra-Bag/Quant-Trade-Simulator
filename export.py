import pandas as pd
import json
import base64
import io
import datetime

def export_orderbook_to_csv(orderbook_data):
    """
    Export orderbook data to CSV format.
    
    Args:
        orderbook_data (dict): Orderbook data with bids and asks
        
    Returns:
        str: Base64 encoded CSV data
    """
    if not orderbook_data or 'bids' not in orderbook_data or 'asks' not in orderbook_data:
        return None
    
    # Create dataframes for bids and asks
    bids_df = pd.DataFrame(orderbook_data['bids'], columns=['price', 'size'])
    asks_df = pd.DataFrame(orderbook_data['asks'], columns=['price', 'size'])
    
    # Add type column
    bids_df['type'] = 'bid'
    asks_df['type'] = 'ask'
    
    # Combine into one dataframe
    combined_df = pd.concat([bids_df, asks_df])
    
    # Add timestamp and symbol
    combined_df['timestamp'] = orderbook_data.get('timestamp', '')
    combined_df['symbol'] = orderbook_data.get('symbol', '')
    
    # Create a buffer
    buffer = io.StringIO()
    
    # Write to CSV
    combined_df.to_csv(buffer, index=False)
    
    # Get the content as string
    csv_string = buffer.getvalue()
    
    # Encode as base64
    csv_base64 = base64.b64encode(csv_string.encode()).decode()
    
    return csv_base64

def export_orderbook_to_excel(orderbook_data):
    """
    Export orderbook data to Excel format.
    
    Args:
        orderbook_data (dict): Orderbook data with bids and asks
        
    Returns:
        str: Base64 encoded Excel data
    """
    if not orderbook_data or 'bids' not in orderbook_data or 'asks' not in orderbook_data:
        return None
    
    # Create dataframes for bids and asks
    bids_df = pd.DataFrame(orderbook_data['bids'], columns=['price', 'size'])
    asks_df = pd.DataFrame(orderbook_data['asks'], columns=['price', 'size'])
    
    # Add type column
    bids_df['type'] = 'bid'
    asks_df['type'] = 'ask'
    
    # Create a buffer
    buffer = io.BytesIO()
    
    # Create ExcelWriter object
    with pd.ExcelWriter(buffer, engine='xlsxwriter') as writer:
        # Write each dataframe to a different worksheet
        bids_df.to_excel(writer, sheet_name='Bids', index=False)
        asks_df.to_excel(writer, sheet_name='Asks', index=False)
        
        # Create metadata sheet
        metadata = pd.DataFrame({
            'Property': ['Symbol', 'Timestamp', 'Export Time'],
            'Value': [
                orderbook_data.get('symbol', ''),
                orderbook_data.get('timestamp', ''),
                datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            ]
        })
        metadata.to_excel(writer, sheet_name='Metadata', index=False)
        
        # Access workbook and worksheet objects
        workbook = writer.book
        
        # Add some formatting
        header_format = workbook.add_format({
            'bold': True,
            'bg_color': '#D3D3D3',
            'border': 1
        })
        
        # Add header formatting to all sheets
        for sheet_name in ['Bids', 'Asks', 'Metadata']:
            worksheet = writer.sheets[sheet_name]
            for col_num, value in enumerate(writer.sheets[sheet_name].table[0]):
                worksheet.write(0, col_num, value, header_format)
    
    # Get the content
    excel_data = buffer.getvalue()
    
    # Encode as base64
    excel_base64 = base64.b64encode(excel_data).decode()
    
    return excel_base64 