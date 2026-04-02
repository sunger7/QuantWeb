from django.contrib import admin
from django.urls import path
from . import views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', views.index, name='index'),
    path('update_data/', views.update_data, name='update_data'),
    path('update_board_data/', views.update_board_data, name='update_board_data'),
    path('start_board_update/', views.start_board_update, name='start_board_update'),
    path('board_update_status/', views.board_update_status, name='board_update_status'),
    path('settings/', views.settings_view, name='settings'),
    path('task_status/', views.task_status, name='task_status'),
    path('start_analysis_task/<str:strategy_id>/', views.start_analysis_task, name='start_analysis_task'),
    path('start_recalc_task/<str:strategy_id>/<str:stock_code>/', views.start_recalc_task, name='start_recalc_task'),
    path('strategy/<str:strategy_id>/', views.strategy_analysis, name='strategy_analysis'),
    path('strategy/<str:strategy_id>/<str:stock_code>/', views.strategy_detail, name='strategy_detail'),
    path('report/<str:strategy_id>/<str:stock_code>/', views.serve_report, name='serve_report'),
    path('stocks/', views.stock_select, name='stock_select'),
    path('stocks/csi300/', views.csi300_stock_select, name='csi300_stock_select'),
    path('watchlist_api/', views.watchlist_api, name='watchlist_api'),
    path('update_single_stock/', views.update_single_stock, name='update_single_stock'),
    path('update_watchlist_data/', views.update_watchlist_data, name='update_watchlist_data'),
    path('recalc_watchlist_ml/', views.recalc_watchlist_ml, name='recalc_watchlist_ml'),
    path('board_components_api/', views.board_components_api, name='board_components_api'),
    path('ai_stock_guide/', views.ai_stock_guide, name='ai_stock_guide'),
    path('ml_daily_prediction/', views.ml_daily_prediction, name='ml_daily_prediction'),
    path('ml_trade_simulation/', views.ml_trade_simulation, name='ml_trade_simulation'),
    path('stop_update/', views.stop_update, name='stop_update'),
    path('watchlist/<str:strategy_id>/', views.watchlist_view, name='watchlist'),
    path('ollama_debate/', views.ollama_debate, name='ollama_debate'),
    path('get_kline_data/', views.get_kline_data, name='get_kline_data'),
]
