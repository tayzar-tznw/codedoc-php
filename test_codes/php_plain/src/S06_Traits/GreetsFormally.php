<?php
declare(strict_types=1);

namespace App\S06_Traits;

trait GreetsFormally
{
    public function hello(): string
    {
        return 'good day';
    }
}
