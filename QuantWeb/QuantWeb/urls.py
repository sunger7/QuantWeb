from django.contrib import admin
from django.urls import path
from . import views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', views.index, name='index'),
    path('update_data/', views.update_data, name='update_data'),
    path('update_today_data/', views.update_today_data, name='update_today_data'),
    path('settings/', views.settings_view, name='settings'),
    path('task_status/', views.task_status, name='task_status'),
    path('start_analysis_task/<str:strategy_id>/', views.start_analysis_task, name='start_analysis_task'),
    path('start_recalc_task/<str:strategy_id>/<str:stock_code>/', views.start_recalc_task, name='start_recalc_task'),
    path('strategy/<str:strategy_id>/', views.strategy_analysis, name='strategy_analysis'),
    path('strategy/<str:strategy_id>/<str:stock_code>/', views.strategy_detail, name='strategy_detail'),
    path('report/<str:strategy_id>/<str:stock_code>/', views.serve_report, name='serve_report'),
    path('stocks/', views.stock_select, name='stock_select'),
    path('watchlist_api/', views.watchlist_api, name='watchlist_api'),
    path('update_single_stock/', views.update_single_stock, name='update_single_stock'),
    path('update_watchlist_data/', views.update_watchlist_data, name='update_watchlist_data'),
    path('stop_update/', views.stop_update, name='stop_update'),
    path('watchlist/<str:strategy_id>/', views.watchlist_view, name='watchlist'),
]
