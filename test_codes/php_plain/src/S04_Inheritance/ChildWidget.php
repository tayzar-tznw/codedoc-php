<?php
declare(strict_types=1);

namespace App\S04_Inheritance;

class ChildWidget extends ParentWidget
{
    public function describe(): string
    {
        return 'child:' . parent::describe();
    }

    public function combined(): string
    {
        return $this->describe() . '|' . $this->format();
    }
}
