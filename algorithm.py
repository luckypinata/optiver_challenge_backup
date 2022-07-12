import datetime as dt
import time
import logging

from optibook.synchronous_client import Exchange

from math import floor, ceil
from black_scholes import call_value, put_value, call_delta, put_delta
from libs import calculate_current_time_to_date

exchange = Exchange()
exchange.connect()

logging.getLogger('client').setLevel('ERROR')


def round_down_to_tick(price, tick_size):
    """
    Rounds a price down to the nearest tick, e.g. if the tick size is 0.10, a price of 0.97 will get rounded to 0.90.
    """
    return floor(price / tick_size) * tick_size


def round_up_to_tick(price, tick_size):
    """
    Rounds a price up to the nearest tick, e.g. if the tick size is 0.10, a price of 1.34 will get rounded to 1.40.
    """
    return ceil(price / tick_size) * tick_size


def get_midpoint_value(instrument_id):
    """
    This function calculates the current midpoint of the order book supplied by the exchange for the instrument
    specified by <instrument_id>, returning None if either side or both sides do not have any orders available.
    """
    order_book = exchange.get_last_price_book(instrument_id=instrument_id)

    # If the instrument doesn't have prices at all or on either side, we cannot calculate a midpoint and return None
    if not (order_book and order_book.bids and order_book.asks):
        return None
    else:
        midpoint = (order_book.bids[0].price + order_book.asks[0].price) / 2.0
        return midpoint


def calculate_theoretical_option_value(expiry_date, strike, callput, stock_value, interest_rate, volatility):
    """
    This function calculates the current fair call or put value based on Black & Scholes assumptions.

    expiry_date: dt.date     -  Expiry date of the option
    strike: float            -  Strike price of the option
    callput: str             -  String 'call' or 'put' detailing what type of option this is
    stock_value:             -  Assumed stock value when calculating the Black-Scholes value
    interest_rate:           -  Assumed interest rate when calculating the Black-Scholes value
    volatility:              -  Assumed volatility of when calculating the Black-Scholes value
    """
    time_to_expiry = calculate_current_time_to_date(expiry_date)

    if callput == 'call':
        option_value = call_value(S=stock_value, K=strike, T=time_to_expiry, r=interest_rate, sigma=volatility)
    elif callput == 'put':
        option_value = put_value(S=stock_value, K=strike, T=time_to_expiry, r=interest_rate, sigma=volatility)
    else:
        raise Exception(f"""Got unexpected value for callput argument, should be 'call' or 'put' but was {callput}.""")

    return option_value


def calculate_option_delta(expiry_date, strike, callput, stock_value, interest_rate, volatility):
    """
    This function calculates the current option delta based on Black & Scholes assumptions.

    expiry_date: dt.date     -  Expiry date of the option
    strike: float            -  Strike price of the option
    callput: str             -  String 'call' or 'put' detailing what type of option this is
    stock_value:             -  Assumed stock value when calculating the Black-Scholes value
    interest_rate:           -  Assumed interest rate when calculating the Black-Scholes value
    volatility:              -  Assumed volatility of when calculating the Black-Scholes value
    """
    time_to_expiry = calculate_current_time_to_date(expiry_date)

    if callput == 'call':
        option_value = call_delta(S=stock_value, K=strike, T=time_to_expiry, r=interest_rate, sigma=volatility)
    elif callput == 'put':
        option_value = put_delta(S=stock_value, K=strike, T=time_to_expiry, r=interest_rate, sigma=volatility)
    else:
        raise Exception(f"""Got unexpected value for callput argument, should be 'call' or 'put' but was {callput}.""")

    return option_value


def update_quotes(option_id, theoretical_price, credit, volume, position_limit, tick_size):
    """
    This function updates the quotes specified by <option_id>. We take the following actions in sequence:
        - pull (remove) any current oustanding orders
        - add credit to theoretical price and round to nearest tick size to create a set of bid/ask quotes
        - calculate max volumes to insert as to not pass the position_limit
        - reinsert limit orders on those levels

    Arguments:
        option_id: str           -  Exchange Instrument ID of the option to trade
        theoretical_price: float -  Price to quote around
        credit: float            -  Difference to subtract from/add to theoretical price to come to final bid/ask price
        volume:                  -  Volume (# lots) of the inserted orders (given they do not breach position limits)
        position_limit: int      -  Position limit (long/short) to avoid crossing
        tick_size: float         -  Tick size of the quoted instrument
    """

    # Print any new trades
    trades = exchange.poll_new_trades(instrument_id=option_id)
    for trade in trades:
        print(f'- Last period, traded {trade.volume} lots in {option_id} at price {trade.price:.2f}, side {trade.side}.')

    # Pull (remove) all existing outstanding orders
    orders = exchange.get_outstanding_orders(instrument_id=option_id)
    for order_id, order in orders.items():
        print(f'- Deleting old {order.side} order in {option_id} for {order.volume} @ {order.price:8.2f}.')
        exchange.delete_order(instrument_id=option_id, order_id=order_id)

    # Calculate bid and ask price
    bid_price = round_down_to_tick(theoretical_price - credit, tick_size)
    ask_price = round_up_to_tick(theoretical_price + credit, tick_size)

    # Calculate bid and ask volumes, taking into account the provided position_limit
    position = exchange.get_positions()[option_id]

    max_volume_to_buy = position_limit - position
    max_volume_to_sell = position_limit + position

    bid_volume = min(volume, max_volume_to_buy)
    ask_volume = min(volume, max_volume_to_sell)

    # Insert new limit orders
    if bid_volume > 0:
        print(f'- Inserting bid limit order in {option_id} for {bid_volume} @ {bid_price:8.2f}.')
        exchange.insert_order(
            instrument_id=option_id,
            price=bid_price,
            volume=bid_volume,
            side='bid',
            order_type='limit',
        )
    if ask_volume > 0:
        print(f'- Inserting ask limit order in {option_id} for {ask_volume} @ {ask_price:8.2f}.')
        exchange.insert_order(
            instrument_id=option_id,
            price=ask_price,
            volume=ask_volume,
            side='ask',
            order_type='limit',
        )


def hedge_delta_position(stock_id, options, stock_value):
    """
    This function (once finished) hedges the outstanding delta position by trading in the stock.

    That is:
        - It calculates how sensitive the total position value is to changes in the underlying by summing up all
          individual delta component.
        - And then trades stocks which have the opposite exposure, to remain, roughly, flat delta exposure

    Arguments:
        stock_id: str         -  Exchange Instrument ID of the stock to hedge with
        options: List[dict]   -  List of options with details to calculate and sum up delta positions for
        stock_value: float    -  The stock value to assume when making delta calculations using Black-Scholes
    """

    # A3: Calculate the delta position here
    positions = exchange.get_positions()

    for option in options:
        position = positions[option['id']]
        print(f"- The current position in option {option['id']} is {position}.")

    stock_position = positions[stock_id]
    print(f'- The current position in the stock {stock_id} is {stock_position}.')

    # A4: Implement the delta hedge here, staying mindful of the overall position-limit of 100, also for the stocks.
    print(f'- Delta hedge not implemented. Doing nothing.')


# A2: Not all the options have been entered here yet, include all of them for an easy improvement
STOCK_ID = 'BMW'
OPTIONS = [
    {'id': 'BMW-2021_12_10-075C', 'expiry_date': dt.datetime(2021, 12, 10, 12, 0, 0), 'strike': 75, 'callput': 'call'},
    {'id': 'BMW-2021_12_10-075P', 'expiry_date': dt.datetime(2021, 12, 10, 12, 0, 0), 'strike': 75, 'callput': 'put'},
    {'id': 'BMW-2022_01_14-075C', 'expiry_date': dt.datetime(2022,  1, 14, 12, 0, 0), 'strike': 75, 'callput': 'call'},
    {'id': 'BMW-2022_01_14-075P', 'expiry_date': dt.datetime(2022,  1, 14, 12, 0, 0), 'strike': 75, 'callput': 'put'},
]

while True:
    print(f'')
    print(f'-----------------------------------------------------------------')
    print(f'TRADE LOOP ITERATION ENTERED AT {str(dt.datetime.now()):18s} UTC.')
    print(f'-----------------------------------------------------------------')

    stock_value = get_midpoint_value(STOCK_ID)
    if stock_value is None:
        print('Empty stock order book on bid or ask-side, or both, unable to update option prices.')
        time.sleep(4)
        continue

    for option in OPTIONS:
        print(f"\nUpdating instrument {option['id']}")

        theoretical_value = calculate_theoretical_option_value(expiry_date=option['expiry_date'],
                                                               strike=option['strike'],
                                                               callput=option['callput'],
                                                               stock_value=stock_value,
                                                               interest_rate=0.0,
                                                               volatility=3.0)

        # A1: Here we ask a fixed credit of 15cts, regardless of what the market circumstances are or which option
        #  we're quoting. That can be improved. Can you think of something better?
        # A5: Here we are inserting a volume of 3, only taking into account the position limit of 100, are there better
        #  choices?
        update_quotes(option_id=option['id'],
                      theoretical_price=theoretical_value,
                      credit=0.15,
                      volume=3,
                      position_limit=100,
                      tick_size=0.10)

        # Wait 1/10th of a second to avoid breaching the exchange frequency limit
        time.sleep(0.10)

    print(f'\nHedging delta position')
    hedge_delta_position(STOCK_ID, OPTIONS, stock_value)

    print(f'\nSleeping for 4 seconds.')
    time.sleep(4)
