<?php
declare(strict_types=1);

namespace App\S06_Traits;

class Order
{
    use LogsActivity;

    public function total(): int
    {
        return 100;
    }
}
