import pandas as pd
from typing import Dict

def calculate_gex(options_chain: pd.DataFrame, spot_price: float) -> Dict:
    """
    Calculate Gamma Exposure (GEX) for SPX/SPY.
    Formula: GEX = Gamma × Open_Interest × Contract_Size × Spot² × 0.01
    """
    # Calls: dealers short gamma (negative contribution)
    call_gex = (options_chain[options_chain['type'] == 'call']['gamma']
                * options_chain['open_interest']
                * 100 * spot_price**2 * 0.01 * -1).sum()

    # Puts: dealers long gamma (positive contribution)
    put_gex = (options_chain[options_chain['type'] == 'put']['gamma']
               * options_chain['open_interest']
               * 100 * spot_price**2 * 0.01).sum()

    net_gex = call_gex + put_gex

    call_strikes = (options_chain[options_chain['type'] == 'call']
                    .groupby('strike')
                    .apply(lambda x: (x['gamma'] * x['open_interest']).sum())
                    .abs().sort_values(ascending=False))

    put_strikes  = (options_chain[options_chain['type'] == 'put']
                    .groupby('strike')
                    .apply(lambda x: (x['gamma'] * x['open_interest']).sum())
                    .abs().sort_values(ascending=False))

    return {
        'net_gex':    net_gex,
        'call_gex':   call_gex,
        'put_gex':    put_gex,
        'call_wall':  call_strikes.index[0] if len(call_strikes) > 0 else None,
        'put_wall':   put_strikes.index[0]  if len(put_strikes)  > 0 else None,
        'zero_gamma': spot_price,
        'regime':     'POSITIVE_GAMMA' if net_gex > 0 else 'NEGATIVE_GAMMA'
    }
